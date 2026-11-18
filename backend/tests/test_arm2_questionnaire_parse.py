import io
import zipfile

import pytest
from test_arm2_questionnaire_workflow import transport, workbook

from app.arm2.questionnaire_parse import MAX_COMPRESSED, MAX_ENTRIES, parse


def test_thousand_formula_rows_are_streamed_and_never_executed():
    data = workbook(rows=[{'B': '=CONCAT("not executed")', 'I': f'A{i:03}AA86', 'J': f'Row {i}'} for i in range(1000)])
    payload, attachments = parse(data, 'synthetic.xlsx')
    assert len(payload['vehicles']) == 1000 and not attachments
    assert all(not row['id'] for row in payload['vehicles'])
    assert payload['vehicles'][-1]['name'] == 'Row 999'


def test_over_one_thousand_vehicle_rows_is_rejected():
    with pytest.raises(ValueError, match='1000'):
        parse(workbook(rows=[{'I': 'A123AA86'}] * 1001), 'synthetic.xlsx')


def test_32_mib_transport_limit_is_checked_before_reading_container():
    with pytest.raises(ValueError, match='32'):
        parse(b'x' * (MAX_COMPRESSED + 1), 'oversize.zip')


def test_more_than_256_entries_is_rejected():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        for i in range(MAX_ENTRIES + 1):
            z.writestr(str(i), b'')
    with pytest.raises(ValueError, match='лимит'):
        parse(out.getvalue(), 'entries.zip')


def test_zip_bomb_ratio_is_rejected_without_unpacking_nested_payload():
    content = transport(workbook(), {'bomb.pdf': b'x' * 5_000_000})
    with pytest.raises(ValueError, match='бомба'):
        parse(content, 'bomb.zip')


@pytest.mark.parametrize('name', ['C:/absolute.pdf', 'deep/a/b/c/file.pdf', 'bad\\windows.pdf'])
def test_additional_unsafe_paths_are_rejected(name):
    data = transport(workbook(), {name: b'bad'})
    if '\\' in name:
        # Windows ZipInfo normalizes os.sep on write; put the hostile spelling into
        # both raw ZIP name headers so the reader, rather than the fixture, sees it.
        data = data.replace(name.replace('\\', '/').encode(), name.encode())
    with pytest.raises(ValueError):
        parse(data, 'unsafe.zip')


def test_invalid_date_is_visible_warning_not_invented_date_or_blocker():
    parsed, _ = parse(workbook({'B4': 'not a date', 'B18': 'Still useful'}), 'synthetic.xlsx')
    assert parsed['filled_date'] is None and parsed['warnings'] and parsed['cp']['phone'] == 'Still useful'


def test_formula_payload_is_not_a_parser_for_arbitrary_details_keys():
    parsed, _ = parse(workbook({'A13': 'inn:ИНН', 'B7': 'details', 'B18': '=HYPERLINK("https://invalid")'}), 'synthetic.xlsx')
    assert parsed['cp']['phone'] == '' and 'details' not in parsed['cp'] and 'inn:ИНН' not in parsed['cp']
