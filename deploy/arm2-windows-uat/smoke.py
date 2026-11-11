"""Optional, MUTATING UAT-only API smoke. Run later ONLY with owner approval.

Creates fictitious records, never deletes them, never logs passwords/cookies.
This is not a Windows GUI test. Does not start any server or integration.
"""

import getpass
import secrets

import httpx
from uat_policy import PACKAGE, REVISION, SOURCE, validate_environment


def main():
    validate_environment()
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=15, trust_env=False) as client:
        health = client.get("/api/health")
        health.raise_for_status()
        identity = health.json()
        if (identity.get("package"), identity.get("source_sha"), identity.get("migration")) != (PACKAGE, SOURCE, REVISION):
            raise SystemExit("Wrong UAT instance; no credentials sent or records created")
        response = client.post("/api/auth/login", json={"login": "uat-admin", "password": getpass.getpass("UAT admin password: ")})
        if response.status_code != 200:
            raise SystemExit("UAT login failed (response suppressed)")
        client.headers["X-CSRF-Token"] = client.cookies["csrf"]

        def request(method, path, body=None, status=200):
            result = client.request(method, "/api" + path, json=body)
            if result.status_code != status:
                raise RuntimeError(f"UAT smoke failed: {method} {path}, HTTP {result.status_code}; response suppressed")
            return result.json()

        suffix = secrets.token_hex(5)
        accounts = [request("POST", "/users", {"login": f"uat-smoke-{suffix}-{letter}", "name": f"UAT SMOKE {letter}",
                    "password": secrets.token_urlsafe(24), "role": "OPERATOR", "active": False,
                    "permissions": [], "allowed_chat_ids": []}, 201) for letter in ("a", "b")]
        account_ids = [account["id"] for account in accounts]
        before = {user["id"]: (user["role"], user["permissions"]) for user in request("GET", "/users")}
        missing_user = max(before) + 100000
        type_record = request("POST", "/arm2/types", {"name": f"UAT SMOKE TYPE {suffix}"}, 201)
        assert type_record["status"] == "REVIEW"
        type_path = "/arm2/types/" + type_record["id"]
        type_record = request("PUT", type_path, {"name": type_record["name"], "expected_version": type_record["version"],
                              "requires_sts": True, "requires_calibration": False, "requires_wialon": True})
        assert request("GET", type_path)["status"] == "CONFIRMED"
        parties = {}
        rep_keys = ("name", "position", "authority_basis", "phone", "email", "state", "user_id", "last_name", "first_name", "middle_name")
        for kind, collection in (("CP", "counterparties"), ("ORG", "organizations")):
            parent = request("POST", f"/arm2/{collection}", {"name": f"UAT SMOKE {kind} {suffix}"}, 201)
            parent_path = f"/arm2/{collection}/{parent['id']}"
            parent = request("PUT", parent_path, {"name": parent["name"], "expected_version": parent["version"],
                             "full_name": "Fictitious UAT party", "legal_address": "Fictitious UAT address"})
            assert request("GET", parent_path)["status"] == "CONFIRMED"
            rep_collection = parent_path + "/representatives"
            rep = request("POST", rep_collection, {"name": f"UAT SMOKE {kind}REP", "user_id": account_ids[0]}, 201)
            rep_path = rep_collection + "/" + rep["id"]
            for expected_version, link in enumerate([account_ids[0], account_ids[1], None], 1):
                if expected_version > 1:
                    rep = request("PUT", rep_path, {key: rep[key] for key in rep_keys} | {"user_id": link, "expected_version": rep["version"]})
                rep = request("GET", rep_path)
                assert rep["user_id"] == link and rep["version"] == expected_version
                assert link is None or type(rep["user_id"]) is int
                events = request("GET", f"/arm2/history/{kind}REP/{rep['id']}")
                event = next(item for item in events if item["version_after"] == expected_version)
                changes = request("GET", f"/arm2/history/events/{event['event_id']}")["changes"]
                assert changes["user_id"] == {"before": None if expected_version == 1 else account_ids[expected_version - 2], "after": link}
            body = {key: rep[key] for key in rep_keys} | {"expected_version": rep["version"]}
            assert request("PUT", rep_path, body)["version"] == rep["version"]
            request("PUT", rep_path, body | {"expected_version": 1, "user_id": account_ids[0]}, status=409)
            request("PUT", rep_path, body | {"user_id": missing_user}, status=422)
            assert request("GET", rep_path) == rep
            parent = request("PUT", parent_path, {"name": parent["name"], "expected_version": parent["version"], "primary_signer_id": rep["id"]})
            assert request("GET", parent_path)["primary_signer_id"] == rep["id"]
            parties[kind] = parent["id"]
        after = {user["id"]: (user["role"], user["permissions"]) for user in request("GET", "/users")}
        assert before == after
        letters = "АВЕКМНОРСТУХ"
        plate = secrets.choice(letters) + f"{secrets.randbelow(1000):03}" + secrets.choice(letters) + secrets.choice(letters) + "86"
        vehicle = request("POST", "/arm2/vehicles", {"name": f"UAT SMOKE VEH {suffix}", "plate_number": plate,
                          "type_id": type_record["id"], "owner_cp_id": parties["CP"]}, 201)
        vehicle_path = "/arm2/vehicles/" + vehicle["id"]
        vehicle_keys = ("name", "plate_number", "type_id", "accounting_state", "model", "wialon_unit_id", "owner_cp_id", "owner_org_id")
        for owner_cp, owner_org in ((None, parties["ORG"]), (None, None)):
            vehicle = request("PUT", vehicle_path, {key: vehicle[key] for key in vehicle_keys} |
                              {"expected_version": vehicle["version"], "owner_cp_id": owner_cp, "owner_org_id": owner_org})
            actual = request("GET", vehicle_path)
            assert (actual["owner_cp_id"], actual["owner_org_id"]) == (owner_cp, owner_org)
        request("POST", "/webhooks/max", {}, status=404)
        request("GET", "/integration/v1/photos", status=404)
        request("POST", "/auth/logout", {})
        print(f"API_SMOKE_PASS {PACKAGE} source={SOURCE} fixture={suffix}; fictitious data retained; Windows GUI NOT TESTED")


if __name__ == "__main__":
    main()
