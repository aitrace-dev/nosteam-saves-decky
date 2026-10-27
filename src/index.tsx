import { useEffect, useState } from "react";
import { callable, definePlugin, routerHook, toaster } from "@decky/api";
import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { FaCloud } from "react-icons/fa";
import { CloudIntro, PluginTheme, SaveStatus } from "./theme";

type Shortcut = { shortcut_id: string; title: string; registry: string };
type Game = Shortcut & { game_id: string; status: string; original_options: string; removed?: boolean; wrapped_options?: string };
type SignIn = { status: string; url?: string };
type LibraryItem = Shortcut & { key: string; game_id: string | null; status: string; excluded: boolean; removed: boolean };
type Status = { library?: LibraryItem[]; connected: boolean; games: Game[]; pending: (Shortcut & { status: string })[]; sign_in: SignIn };
type Candidate = Shortcut & { game_id: string; roots: string[] };
type Revision = { revision: string; created: string; device: string };

declare const SteamClient: {
  Apps: {
    SetShortcutLaunchOptions(id: number, options: string): void;
    RegisterForAppDetails(id: number, callback: (details: { strShortcutLaunchOptions: string }) => void): { unregister(): void };
  };
  System: { OpenInSystemBrowser(url: string): void };
  GameSessions: { RegisterForAppLifetimeNotifications(callback: (event: { unAppID: number; bRunning: boolean }) => void): { unregister(): void } };
};

const getStatus = callable<[], Status>("get_status");
const signIn = callable<[], SignIn>("sign_in");
const pollSignIn = callable<[], SignIn>("poll_sign_in");
const automaticPlan = callable<[], { ready: Candidate[] }>("automatic_plan");
const enable = callable<[string, string, string, string[], string], { launch_options: string; original_options: string }>("enable");
const cancelEnable = callable<[string], unknown>("cancel_enable");
const disable = callable<[string], unknown>("disable");
const exclude = callable<[string, string], unknown>("exclude");
const include = callable<[string, string], unknown>("include");
const sync = callable<[string], unknown>("sync");
const resolve = callable<[string, string], unknown>("resolve");
const history = callable<[string], Revision[]>("history");
const rollback = callable<[string, string], unknown>("rollback");

function currentOptions(id: number): Promise<string> {
  return new Promise((accept, reject) => {
    let registration: { unregister(): void } | undefined;
    const timer = setTimeout(() => { registration?.unregister(); reject(new Error("Steam is still loading shortcut settings.")); }, 5000);
    registration = SteamClient.Apps.RegisterForAppDetails(id, details => {
      if (typeof details.strShortcutLaunchOptions !== "string") return;
      clearTimeout(timer);
      queueMicrotask(() => registration?.unregister());
      accept(details.strShortcutLaunchOptions);
    });
  });
}
async function setOptions(id: number, options: string) {
  if (!Number.isInteger(id) || id < 0x80000000 || id > 0xffffffff) throw new Error("Only non-Steam shortcuts are supported.");
  SteamClient.Apps.SetShortcutLaunchOptions(id, options);
  for (let attempt = 0; attempt < 5; attempt++) {
    if (await currentOptions(id) === options) return;
    await new Promise(accept => setTimeout(accept, 200));
  }
  throw new Error("Steam did not save shortcut settings. We will retry.");
}
function idle(id: number): boolean {
  try {
    const apps = SteamUIStore.RunningApps;
    return Array.isArray(apps) && !apps.some(app => app.appid === id);
  } catch { return false; } // An unavailable Steam store is never evidence that a game is idle.
}

const selectionStorageKey = "nosteam-saves:selected-game";
let lastSelection = "";
function readSelection() {
  try { return sessionStorage.getItem(selectionStorageKey) || ""; }
  catch { return lastSelection; }
}
function rememberSelection(key: string) {
  lastSelection = key;
  try { sessionStorage.setItem(selectionStorageKey,key); } catch { /* Selection still survives in memory. */ }
}
let backgroundError = "";
let wakeAutomation = () => {};
function Automation() {
  useEffect(() => {
    let active = true, working = false, nextScan = 0;
    async function tick() {
      if (!active || working) return;
      working = true;
      try {
        const status = await getStatus();
        if (!status.connected) {
          if (status.sign_in.status === "waiting") await pollSignIn();
          return;
        }
        if (Date.now() < nextScan) return;
        nextScan = Date.now() + 30000;
        const result = await automaticPlan();
        for (const candidate of result.ready) {
          if (!active) break;
          const id = Number(candidate.shortcut_id);
          if (!idle(id)) continue;
          const original = await currentOptions(id);
          const binding = await enable(candidate.shortcut_id, candidate.registry, candidate.game_id, candidate.roots, original);
          try { await setOptions(id, binding.launch_options); }
          catch (failure) {
            // Remove a failed binding only after restoring Steam settings successfully.
            await setOptions(id, binding.original_options);
            await cancelEnable(candidate.game_id);
            throw failure;
          }
          if (active && idle(id)) {
            try { await sync(candidate.game_id); } catch { /* Conflicts/offline remain in durable status. */ }
          }
        }
        backgroundError = "";
      } catch {
        backgroundError = "Cloud setup will retry automatically. Your local saves are kept.";
      } finally { working = false; }
    }
    wakeAutomation = () => { nextScan = 0; void tick(); };
    const timer = setInterval(() => void tick(), 5000);
    const events = SteamClient.GameSessions?.RegisterForAppLifetimeNotifications?.(event => {
      if (!event.bRunning && event.unAppID >= 0x80000000) wakeAutomation();
    });
    void tick();
    return () => { active = false; clearInterval(timer); events?.unregister(); wakeAutomation = () => {}; };
  }, []);
  return null;
}

const labels: Record<string, string> = {
  ready: "Preparing cloud saves", checking: "Checking saves…", downloading: "Downloading saves…",
  uploading: "Uploading saves…", synced: "Up to date", playing: "In game", pending: "Upload queued",
  offline: "Offline · uploads will retry", conflict: "Choose your progress", saving: "Saving…",
  recovering: "Recovering saves…", recovered: "Saves recovered", interrupted: "Last session interrupted",
  finding_saves: "Finding saves with the server · retries automatically", identifying: "Identifying the game",
  waiting_for_save: "Waiting for your first in-game save", error: "Sync will retry",
  excluded: "Excluded from cloud sync", removed: "Removed from Steam · backups kept",
};
function Content() {
  const [status, setStatus] = useState<Status>();
  const [error, setError] = useState("");
  const [refreshError, setRefreshError] = useState("");
  const [busy, setBusy] = useState(false);
  const [selectedKey, setSelectedKey] = useState(readSelection);
  const [revisions, setRevisions] = useState<Record<string, Revision[]>>({});
  const [selectedRevision, setSelectedRevision] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState("");
  const library: LibraryItem[] = status?.library ?? status?.games.map(game => ({...game, key:game.game_id,excluded:false,removed:false})) ?? [];
  const item = library.find(game => game.key === selectedKey) ?? library[0];
  const game = status?.games.find(game => game.game_id === item?.game_id);
  const running = game?.status === "playing";
  async function refresh() { setStatus(await getStatus()); }
  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { await action(); await refresh(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Could not finish this action. Try again."); }
    finally { setBusy(false); }
  }
  useEffect(() => {
    let active = true;
    const update = () => getStatus().then(value => { if (active) { setStatus(value); setRefreshError(""); } }).catch(() => {
      if (active) setRefreshError("Cloud saves are reconnecting. We’ll retry automatically.");
    });
    void update(); const timer = setInterval(() => void update(), 5000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  return <div className="nss-root"><PluginTheme/>
    <CloudIntro connected={status?.connected} games={status?.games.filter(game => !game.removed).length} pending={library.filter(game => !game.game_id && !game.excluded).length}/>
    {(error || refreshError) && <PanelSection><PanelSectionRow><div className="nss-alert" role="alert">{error || refreshError}</div></PanelSectionRow></PanelSection>}
    {!status ? <div className="nss-block"><PanelSection title="Loading your saves"><PanelSectionRow><SaveStatus status="checking" label="Checking your account…"/></PanelSectionRow></PanelSection></div> : !status.connected ? <div className="nss-block nss-account"><PanelSection title="Your account">
      <PanelSectionRow><div className="nss-copy">Sign in once. This Bazzite connects to your account and finds your saves automatically.</div></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none" disabled={busy} onClick={() => perform(async () => {
        const result = await signIn();
        if (result.url) SteamClient.System.OpenInSystemBrowser(result.url);
        wakeAutomation();
      })}>{status?.sign_in.status === "waiting" ? "Continue sign-in" : "Sign in"}</ButtonItem></PanelSectionRow>
      {status?.sign_in.status === "waiting" && <PanelSectionRow><div className="nss-copy">Choose “Confirm sign-in” in your browser.</div></PanelSectionRow>}
    </PanelSection></div> : <>
      <div className="nss-block nss-overview"><PanelSection title="Cloud connection">
        <PanelSectionRow><SaveStatus status="connected" label="Connected · automatic sync"/></PanelSectionRow>
        {backgroundError && <PanelSectionRow><div className="nss-alert">{backgroundError}</div></PanelSectionRow>}
      </PanelSection></div>
      <div className={`nss-block ${item?.status === "conflict" ? "nss-game-conflict" : "nss-game"}`}><PanelSection title="Your games">
        {!library.length ? <PanelSectionRow><div className="nss-copy">No non-Steam games found yet. New shortcuts appear automatically.</div></PanelSectionRow> : <>
          <PanelSectionRow><DropdownItem layout="below" label="Game" selectedOption={item.key}
            rgOptions={library.map(game => ({data:game.key,label:game.title + (game.excluded ? " · excluded" : game.removed ? " · removed" : "")}))}
            onChange={option => {rememberSelection(String(option.data));setSelectedKey(String(option.data));setConfirm("");}}/></PanelSectionRow>
          <PanelSectionRow><SaveStatus status={item.status} label={labels[item.status] || "Preparing cloud saves"}/></PanelSectionRow>
          {!item.game_id && !item.excluded && <PanelSectionRow><div className="nss-copy">We check known save locations first. Unresolved searches retry with limited AI assistance.</div></PanelSectionRow>}
          {item.removed && <PanelSectionRow><div className="nss-copy">Sync stops when a shortcut is removed. Your previous backups are kept in Your Saves.</div></PanelSectionRow>}
          {game && !item.removed && <>
            {game.status === "conflict" && <>
              <PanelSectionRow><div className="nss-copy">Both devices have different progress. Both copies are kept.</div></PanelSectionRow>
              <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none" disabled={busy} onClick={() => perform(() => resolve(game.game_id,"local"))}>Use this device’s progress</ButtonItem></PanelSectionRow>
              <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none" disabled={busy} onClick={() => perform(() => resolve(game.game_id,"cloud"))}>Use cloud progress</ButtonItem></PanelSectionRow>
            </>}
            <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none" disabled={busy || running} onClick={() => perform(async () => {
              const rows = await history(game.game_id);
              setRevisions(previous => ({...previous,[game.game_id]:rows}));
            })}>Save history</ButtonItem></PanelSectionRow>
            {revisions[game.game_id] && <>
              <PanelSectionRow><DropdownItem layout="below" label="Previous save" selectedOption={selectedRevision[game.game_id]}
                rgOptions={revisions[game.game_id].map(revision => ({data:revision.revision,label:new Date(revision.created).toLocaleString()}))}
                onChange={option => {setSelectedRevision({...selectedRevision,[game.game_id]:String(option.data)});setConfirm("");}}/></PanelSectionRow>
              <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none" disabled={busy || running || !selectedRevision[game.game_id]} onClick={() => {
                if (confirm !== game.game_id) {setConfirm(game.game_id);return;}
                void perform(async () => {await rollback(game.game_id,selectedRevision[game.game_id]);setConfirm("");});
              }}>{confirm === game.game_id ? "Confirm restore · current save is backed up" : "Restore previous save"}</ButtonItem></PanelSectionRow>
            </>}
          </>}
          {!item.removed && <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none" disabled={busy || running} onClick={() => perform(async () => {
            if (item.excluded) await include(item.shortcut_id,item.registry);
            else if (game) {
              if (!idle(Number(game.shortcut_id))) throw new Error("Close the game before excluding it from sync.");
              await setOptions(Number(game.shortcut_id),game.original_options);
              try { await disable(game.game_id); }
              catch (failure) {
                if (game.wrapped_options) await setOptions(Number(game.shortcut_id),game.wrapped_options);
                throw failure;
              }
            } else await exclude(item.shortcut_id,item.registry);
            wakeAutomation();
            toaster.toast({title:item.excluded ? "Cloud saves enabled" : "Excluded from sync",body:"Your saves are kept."});
          })}>{item.excluded ? "Enable cloud saves" : "Exclude from sync"}</ButtonItem></PanelSectionRow>}
        </>}
      </PanelSection></div>
    </>}
  </div>;
}
export default definePlugin(() => {
  routerHook.addGlobalComponent("NoSteamSavesAutomation", Automation);
  return {name:"NoSteam Saves",titleView:<div>NoSteam Saves</div>,content:<Content/>,icon:<FaCloud/>,
    onDismount() { routerHook.removeGlobalComponent("NoSteamSavesAutomation"); }};
});
