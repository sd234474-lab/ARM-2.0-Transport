import {useContext,useEffect,useRef} from 'react';
import {UNSAFE_NavigationContext,useLocation} from 'react-router-dom';

// ARM2-only guard for mounted forms. Callers own their subject draft and callbacks.
export function useDraftGuard(blocked: boolean, park: () => void, discard: () => void, {message = 'Покинуть карточку и потерять несохранённые изменения?', allowSamePathReplace = false}: {message?: string; allowSamePathReplace?: boolean} = {}) {
  const {navigator} = useContext(UNSAFE_NavigationContext);
  const location = useLocation();
  const dirty = useRef(blocked); dirty.current = blocked;
  const callbacks = useRef({park, discard}); callbacks.current = {park, discard};
  const index = useRef<number | undefined>(window.history.state?.idx);
  useEffect(() => {index.current = window.history.state?.idx;}, [location.key]);
  useEffect(() => {
    const mountedHref = window.location.href, mountedPathname = location.pathname;
    const leave = () => !dirty.current || window.confirm(message);
    const push = navigator.push, replace = navigator.replace, go = navigator.go;
    const guardedPush: typeof push = (...args) => {if (leave()) {dirty.current = false; push.apply(navigator, args);}};
    const guardedReplace: typeof replace = (...args) => {const to=args[0];if(allowSamePathReplace&&typeof to!=='string'&&(!to.pathname||to.pathname===mountedPathname)){replace.apply(navigator,args);return;}if (leave()) {dirty.current = false; replace.apply(navigator, args);}};
    const guardedGo: typeof go = (...args) => {if (leave()) {dirty.current = false; go.apply(navigator, args);}};
    navigator.push = guardedPush; navigator.replace = guardedReplace; navigator.go = guardedGo;
    let restoring = false, unmounted = false;
    const remove = () => {window.removeEventListener('popstate', pop, true); window.removeEventListener('beforeunload', unload);};
    const pop = (event: PopStateEvent) => {
      if (restoring) {restoring = false; if (unmounted) remove(); else event.stopImmediatePropagation(); return;}
      const next = event.state?.idx;
      if (dirty.current && typeof next === 'number' && typeof index.current === 'number' && next !== index.current) {
        if (!leave()) {event.stopImmediatePropagation(); restoring = true; window.history.go(index.current - next); return;}
        dirty.current = false; callbacks.current.discard();
      }
      index.current = next;
    };
    const unload = (event: BeforeUnloadEvent) => {if (dirty.current) {event.preventDefault(); event.returnValue = '';}};
    window.addEventListener('popstate', pop, true); window.addEventListener('beforeunload', unload);
    return () => {
      unmounted = true;
      if (navigator.push === guardedPush) navigator.push = push;
      if (navigator.replace === guardedReplace) navigator.replace = replace;
      if (navigator.go === guardedGo) navigator.go = go;
      if (dirty.current && window.location.href !== mountedHref) {
        callbacks.current.park();
        // A Window POP can synchronously unmount us in Router's earlier listener.
        // Finish this event before removing our listener; a refused POP remounts
        // from parked state, while an accepted POP discards it.
        window.setTimeout(() => {if (!restoring) {remove(); callbacks.current.discard();}}, 0);
      } else remove();
    };
  }, [navigator, message, allowSamePathReplace]);
  return dirty;
}
