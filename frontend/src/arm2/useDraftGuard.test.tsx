// @vitest-environment jsdom
import {act,useContext} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter,UNSAFE_NavigationContext,useLocation} from 'react-router-dom';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {DraftScope,useDraftCache,useDraftGuard} from './useDraftGuard';

let root:ReturnType<typeof createRoot>,host:HTMLDivElement;
let navigator:NonNullable<React.ContextType<typeof UNSAFE_NavigationContext>>['navigator'];
let baseline:Pick<typeof navigator,'push'|'replace'|'go'>;
let dirty:ReturnType<typeof useDraftGuard>;
const park=vi.fn(),discard=vi.fn();
function Probe({blocked=true,allow=false}:{blocked?:boolean;allow?:boolean}) {
  const context=useContext(UNSAFE_NavigationContext)!;
  navigator=context.navigator;
  baseline??={push:navigator.push,replace:navigator.replace,go:navigator.go};
  dirty=useDraftGuard(blocked,park,discard,{message:'Leave this ARM2 draft?',allowSamePathReplace:allow});
  return <p>{useLocation().pathname}</p>;
}
beforeEach(()=>{
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
  window.history.replaceState({idx:1},'','/arm2/current');
  host=document.createElement('div');document.body.append(host);root=createRoot(host);
  baseline=undefined as unknown as typeof baseline;park.mockClear();discard.mockClear();
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.restoreAllMocks();window.history.replaceState(null,'','/');});
async function mount(props:{blocked?:boolean;allow?:boolean}={}) {
  await act(async()=>root.render(<MemoryRouter initialEntries={['/arm2/current']}><Probe {...props}/></MemoryRouter>));
}
it('keeps parked process drafts inside one workspace and resets them at a new login scope',async()=>{
  let cache: Map<string,string>;
  function CacheProbe(){cache=useDraftCache<string>();return <span>{cache.get('draft')??'empty'}</span>;}
  await act(async()=>root.render(<DraftScope key="first-login"><CacheProbe/></DraftScope>));
  cache!.set('draft','private operator choice');
  await act(async()=>root.render(<DraftScope key="first-login"><CacheProbe/></DraftScope>));
  expect(host.textContent).toBe('private operator choice');
  await act(async()=>root.render(<DraftScope key="another-login"><CacheProbe/></DraftScope>));
  expect(host.textContent).toBe('empty');
});
it.each(['push','replace','go'] as const)('protects %s and preserves dirty state until an accepted departure',async method=>{
  const confirm=vi.spyOn(window,'confirm').mockReturnValue(false);await mount();
  const leave=()=>method==='go'?navigator.go(-1):navigator[method]('/arm2/next');
  await act(async()=>leave());expect(confirm).toHaveBeenCalledWith('Leave this ARM2 draft?');expect(dirty.current).toBe(true);
  expect(host.textContent).toBe('/arm2/current');confirm.mockReturnValue(true);
  await act(async()=>leave());if(method==='go')expect(dirty.current).toBe(false);else expect(host.textContent).toBe('/arm2/next');
});
it('keeps owner query-only replace as an explicit caller policy',async()=>{
  const confirm=vi.spyOn(window,'confirm').mockReturnValue(false);await mount({allow:true});
  await act(async()=>navigator.replace({pathname:'/arm2/current',search:'?tab=vehicles'}));
  expect(confirm).not.toHaveBeenCalled();expect(dirty.current).toBe(true);
  await act(async()=>navigator.replace({pathname:'/arm2/other'}));expect(confirm).toHaveBeenCalledOnce();
});
it('does not bypass ordinary card replacement or the beforeunload guard',async()=>{
  const confirm=vi.spyOn(window,'confirm').mockReturnValue(false);await mount();
  await act(async()=>navigator.replace({pathname:'/arm2/current',search:'?tab=files'}));expect(confirm).toHaveBeenCalledOnce();
  const event=new Event('beforeunload',{cancelable:true});window.dispatchEvent(event);expect(event.defaultPrevented).toBe(true);
  dirty.current=false;const clean=new Event('beforeunload',{cancelable:true});window.dispatchEvent(clean);expect(clean.defaultPrevented).toBe(false);
});
it('restores cancelled native POP, then discards only on an accepted POP',async()=>{
  const go=vi.spyOn(window.history,'go').mockImplementation(()=>{}),confirm=vi.spyOn(window,'confirm').mockReturnValue(false);
  await mount();window.dispatchEvent(new PopStateEvent('popstate',{state:{idx:0}}));
  expect(go).toHaveBeenCalledWith(1);expect(park).not.toHaveBeenCalled();expect(discard).not.toHaveBeenCalled();
  window.dispatchEvent(new PopStateEvent('popstate',{state:{idx:1}}));expect(confirm).toHaveBeenCalledOnce();
  confirm.mockReturnValue(true);window.dispatchEvent(new PopStateEvent('popstate',{state:{idx:0}}));
  expect(discard).toHaveBeenCalledOnce();expect(dirty.current).toBe(false);
});
it('restores navigator methods and removes both listeners when the form unmounts',async()=>{
  const add=vi.spyOn(window,'addEventListener'),remove=vi.spyOn(window,'removeEventListener');await mount({blocked:false});
  const listeners=add.mock.calls.filter(([name])=>name==='popstate'||name==='beforeunload');expect(listeners).toHaveLength(2);
  await act(async()=>root.render(null));expect(navigator.push).toBe(baseline.push);expect(navigator.replace).toBe(baseline.replace);expect(navigator.go).toBe(baseline.go);
  for(const [name,listener] of listeners)expect(remove.mock.calls.some(([n,l])=>n===name&&l===listener)).toBe(true);
  expect(park).not.toHaveBeenCalled();expect(discard).not.toHaveBeenCalled();
});
