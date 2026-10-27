import {afterEach, beforeEach, expect, test, vi} from 'vitest';
import {act, cleanup, fireEvent, render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type {ReactNode} from 'react';

const api=vi.hoisted(()=>({status:{} as any,calls:new Map<string,ReturnType<typeof vi.fn>>(),options:''}));
vi.mock('@decky/api',()=>({definePlugin:(factory:unknown)=>factory,routerHook:{addGlobalComponent:vi.fn(),removeGlobalComponent:vi.fn()},toaster:{toast:vi.fn()},callable:(name:string)=>{const fn=vi.fn(async()=>name==='get_status'?structuredClone(api.status):name==='history'?[{revision:'older-save',created:'2026-10-27T11:00:00Z'}]:{});api.calls.set(name,fn);return fn;}}));
vi.mock('@decky/ui',()=>({
 PanelSection:({title,children}:any)=><section aria-label={title}>{title&&<h2>{title}</h2>}{children}</section>,
 PanelSectionRow:({children}:{children:ReactNode})=><div>{children}</div>,
 ButtonItem:({children,onClick,disabled,layout}:any)=><button data-layout={layout} disabled={disabled} onClick={onClick}>{children}</button>,
 DropdownItem:({label,selectedOption,rgOptions,onChange}:any)=><label>{label}<select value={selectedOption??''} onChange={event=>onChange({data:event.target.value})}>{rgOptions.map((option:any)=><option key={option.data} value={option.data}>{option.label}</option>)}</select></label>,
}));
import plugin from './index';

beforeEach(()=>{
 const first={key:'first',game_id:'first-game',title:'First game',shortcut_id:'2147484000',registry:'/preview',status:'synced',original_options:'gamemoderun %command%',excluded:false,removed:false};
 const second={...first,key:'second',game_id:'second-game',title:'Second game',shortcut_id:'2147484001'};
 api.options='';
 api.status={connected:true,games:[first,second],pending:[],library:[first,second],sign_in:{status:'signed_out'}};
 vi.stubGlobal('SteamUIStore',{RunningApps:[]});
 vi.stubGlobal('SteamClient',{Apps:{SetShortcutLaunchOptions:vi.fn((_id,options)=>{api.options=options;}),RegisterForAppDetails:vi.fn((_id,callback)=>{queueMicrotask(()=>callback({strShortcutLaunchOptions:api.options}));return{unregister:vi.fn()};})},System:{OpenInSystemBrowser:vi.fn()}});
 for(const fn of api.calls.values())fn.mockClear();
});
afterEach(()=>{cleanup();vi.useRealTimers();vi.unstubAllGlobals();});

test('game dropdown shows controls for only the selected game',async()=>{
 render((plugin as any)().content);
 const selector=await screen.findByRole('combobox',{name:'Game'});
 expect(screen.getAllByRole('button',{name:'Save history'})).toHaveLength(1);
 await userEvent.selectOptions(selector,'second');
 await userEvent.click(screen.getByRole('button',{name:'Save history'}));
 expect(api.calls.get('history')).toHaveBeenCalledWith('second-game');
 for(const control of screen.getAllByRole('button'))expect(control.getAttribute('data-layout')).toBe('below');
});

test('pending apps can be excluded and included without changing Steam launch settings',async()=>{
 api.status={...api.status,games:[],library:[{key:'kodi',game_id:null,title:'Kodi',shortcut_id:'2147484002',registry:'/preview',status:'finding_saves',excluded:false,removed:false}]};
 api.calls.get('exclude')!.mockImplementation(async()=>{api.status.library[0]={...api.status.library[0],excluded:true,status:'excluded'};return{};});
 api.calls.get('include')!.mockImplementation(async()=>{api.status.library[0]={...api.status.library[0],excluded:false,status:'finding_saves'};return{};});
 render((plugin as any)().content);
 await userEvent.click(await screen.findByRole('button',{name:'Exclude from sync'}));
 expect(api.calls.get('exclude')).toHaveBeenCalledWith('2147484002','/preview');
 expect((globalThis as any).SteamClient.Apps.SetShortcutLaunchOptions).not.toHaveBeenCalled();
 await userEvent.click(await screen.findByRole('button',{name:'Enable cloud saves'}));
 expect(api.calls.get('include')).toHaveBeenCalledWith('2147484002','/preview');
});

test('excluding an enabled game restores its launch options before removing the binding',async()=>{
 render((plugin as any)().content);
 await userEvent.click(await screen.findByRole('button',{name:'Exclude from sync'}));
 expect((globalThis as any).SteamClient.Apps.SetShortcutLaunchOptions).toHaveBeenCalledWith(2147484000,'gamemoderun %command%');
 expect(api.calls.get('disable')).toHaveBeenCalledWith('first-game');
});

test('a failed Steam settings update cannot remove the sync binding',async()=>{
 (globalThis as any).SteamClient.Apps.SetShortcutLaunchOptions.mockImplementation(()=>{throw Error('Steam is busy');});
 render((plugin as any)().content);
 await userEvent.click(await screen.findByRole('button',{name:'Exclude from sync'}));
 expect(await screen.findByRole('alert')).toHaveProperty('textContent','Steam is busy');
 expect(api.calls.get('disable')).not.toHaveBeenCalled();
});

test('running games cannot be excluded or restored',async()=>{
 api.status.games[0].status='playing';api.status.library[0].status='playing';
 render((plugin as any)().content);
 expect(await screen.findByRole('button',{name:'Exclude from sync'})).toHaveProperty('disabled',true);
 expect(screen.getByRole('button',{name:'Save history'})).toHaveProperty('disabled',true);
});

test('conflict choice applies only to the selected game',async()=>{
 api.status.games[1].status='conflict';api.status.library[1].status='conflict';
 render((plugin as any)().content);
 await userEvent.selectOptions(await screen.findByRole('combobox',{name:'Game'}),'second');
 await userEvent.click(screen.getByRole('button',{name:'Use cloud progress'}));
 expect(api.calls.get('resolve')).toHaveBeenCalledWith('second-game','cloud');
});

test('restore requires an explicit second confirmation',async()=>{
 render((plugin as any)().content);
 await userEvent.click(await screen.findByRole('button',{name:'Save history'}));
 await userEvent.selectOptions(await screen.findByRole('combobox',{name:'Previous save'}),'older-save');
 await userEvent.click(screen.getByRole('button',{name:'Restore previous save'}));
 expect(api.calls.get('rollback')).not.toHaveBeenCalled();
 await userEvent.click(screen.getByRole('button',{name:/Confirm restore/}));
 expect(api.calls.get('rollback')).toHaveBeenCalledWith('first-game','older-save');
});

test('removed games show retained backups and cannot start sync or exclusion',async()=>{
 api.status.games[0].removed=true;api.status.library[0].removed=true;api.status.library[0].status='removed';
 render((plugin as any)().content);
 await screen.findByText('Removed from Steam · backups kept');
 expect(screen.queryByRole('button',{name:'Exclude from sync'})).toBeNull();
 expect(screen.queryByRole('button',{name:'Save history'})).toBeNull();
});

test('sign-in opens the device URL returned by the agent',async()=>{
 api.status={connected:false,games:[],library:[],pending:[],sign_in:{status:'signed_out'}};
 api.calls.get('sign_in')!.mockResolvedValue({url:'https://nosteam.bluggie.com/pair?code=1234-ABCD'});
 render((plugin as any)().content);
 await userEvent.click(await screen.findByRole('button',{name:'Sign in'}));
 expect((globalThis as any).SteamClient.System.OpenInSystemBrowser).toHaveBeenCalledWith('https://nosteam.bluggie.com/pair?code=1234-ABCD');
 expect(screen.queryByRole('combobox',{name:'Game'})).toBeNull();
});

test('polling preserves the selected game and control node while shortcuts change',async()=>{
 vi.useFakeTimers();
 await act(async()=>{render((plugin as any)().content);});
 const selector=screen.getByRole('combobox',{name:'Game'});
 fireEvent.change(selector,{target:{value:'second'}});
 api.status.library=[...api.status.library,{...api.status.library[0],key:'third',game_id:null,title:'New game'}];
 await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
 expect(screen.getByRole('combobox',{name:'Game'})).toBe(selector);
 expect(selector).toHaveProperty('value','second');
 expect(screen.getByRole('option',{name:'New game'})).toBeTruthy();
 api.status.library=api.status.library.map((item:any)=>item.key==='second'?{...item,removed:true,status:'removed'}:item);
 await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
 expect(screen.getByRole('combobox',{name:'Game'})).toBe(selector);
 expect(screen.getByText('Removed from Steam · backups kept')).toBeTruthy();
});

test('automatic setup installs a verified non-Steam wrapper before initial sync',async()=>{
 const {routerHook}=await import('@decky/api');
 (plugin as any)();
 const Automation=(routerHook.addGlobalComponent as any).mock.calls.at(-1)[1];
 api.calls.get('automatic_plan')!.mockResolvedValue({ready:[{shortcut_id:'2147484000',registry:'/preview',game_id:'first-game',roots:['/saves']}]});
 api.calls.get('enable')!.mockResolvedValue({launch_options:'nosteam launch first-game -- %command%',original_options:''});
 render(<Automation/>);
 await vi.waitFor(()=>expect(api.calls.get('sync')).toHaveBeenCalledWith('first-game'));
 expect((globalThis as any).SteamClient.Apps.SetShortcutLaunchOptions).toHaveBeenCalledWith(2147484000,'nosteam launch first-game -- %command%');
});

test('a backend exclusion failure restores the sync wrapper',async()=>{
 api.status.games[0].wrapped_options='nosteam launch first-game -- %command%';
 api.calls.get('disable')!.mockRejectedValueOnce(Error('Game started while changing settings'));
 render((plugin as any)().content);
 await userEvent.click(await screen.findByRole('button',{name:'Exclude from sync'}));
 expect(await screen.findByRole('alert')).toHaveProperty('textContent','Game started while changing settings');
 expect((globalThis as any).SteamClient.Apps.SetShortcutLaunchOptions).toHaveBeenLastCalledWith(2147484000,'nosteam launch first-game -- %command%');
});

test('loading an existing account never flashes the sign-in button',async()=>{
 let finish:(value:any)=>void=()=>{};
 api.calls.get('get_status')!.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
 render((plugin as any)().content);
 expect(screen.queryByRole('button',{name:'Sign in'})).toBeNull();
 expect(screen.getByText('Checking your account…')).toBeTruthy();
 await act(async()=>{finish(structuredClone(api.status));});
 expect(screen.getByRole('combobox',{name:'Game'})).toBeTruthy();
});

test('a temporary refresh failure recovers without remounting the selected control',async()=>{
 vi.useFakeTimers();
 await act(async()=>{render((plugin as any)().content);});
 const selector=screen.getByRole('combobox',{name:'Game'});
 api.calls.get('get_status')!.mockRejectedValueOnce(Error('Agent restarting'));
 await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
 expect(screen.getByRole('alert').textContent).toContain('reconnecting');
 await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
 expect(screen.queryByRole('alert')).toBeNull();
 expect(screen.getByRole('combobox',{name:'Game'})).toBe(selector);
});
