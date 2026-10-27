import { useEffect, useState } from "react";
import { callable, definePlugin, routerHook, toaster } from "@decky/api";
import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { FaCloud } from "react-icons/fa";

type Shortcut = { shortcut_id: string; title: string; registry: string };
type Game = Shortcut & { game_id: string; status: string; original_options: string; wrapped_options?: string };
type SignIn = { status: string; url?: string };
type Status = { connected: boolean; games: Game[]; pending: (Shortcut & { status: string })[]; sign_in: SignIn };
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
};
function Content() {
  const [status, setStatus] = useState<Status>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revisions, setRevisions] = useState<Record<string, Revision[]>>({});
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState("");
  async function refresh() { setStatus(await getStatus()); }
  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { await action(); await refresh(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Could not finish this action. Try again."); }
    finally { setBusy(false); }
  }
  useEffect(() => {
    let active = true;
    const update = () => getStatus().then(value => { if (active) setStatus(value); }).catch(() => {
      if (active) setError("Cloud saves are starting. Please try again shortly.");
    });
    void update(); const timer = setInterval(() => void update(), 3000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  return <>
    <PanelSection title="NoSteam Saves"><PanelSectionRow>
      <div>All supported non-Steam games sync automatically. Use Play as usual.</div>
    </PanelSectionRow>{error && <PanelSectionRow><div role="alert">{error}</div></PanelSectionRow>}</PanelSection>
    {!status?.connected ? <PanelSection title="Your account">
      <PanelSectionRow><div>Sign in once on this Bazzite. We’ll connect it to your account and find your saves automatically.</div></PanelSectionRow>
      <PanelSectionRow><ButtonItem disabled={busy} onClick={() => perform(async () => {
        const result = await signIn();
        if (result.url) SteamClient.System.OpenInSystemBrowser(result.url);
        wakeAutomation();
      })}>{status?.sign_in.status === "waiting" ? "Continue sign-in" : "Sign in to NoSteam Saves"}</ButtonItem></PanelSectionRow>
      {status?.sign_in.status === "waiting" && <PanelSectionRow><div>Choose “Confirm sign-in” in your browser. This Bazzite will connect automatically.</div></PanelSectionRow>}
    </PanelSection> : <>
      <PanelSection title="Automatic cloud saves">
        <PanelSectionRow><div>Signed in · new non-Steam games are added automatically.</div></PanelSectionRow>
        {backgroundError && <PanelSectionRow><div>{backgroundError}</div></PanelSectionRow>}
        {!status.games.length && !status.pending.length && <PanelSectionRow><div>Looking for non-Steam games…</div></PanelSectionRow>}
        {status.pending.map(game => <PanelSectionRow key={`${game.registry}:${game.shortcut_id}`}><div>
          <strong>{game.title}</strong><div>{labels[game.status] || "Waiting for save discovery"}</div>
        </div></PanelSectionRow>)}
      </PanelSection>
      {status.games.map(game => <PanelSection key={game.game_id} title={game.title}>
        <PanelSectionRow><div aria-live="polite">{labels[game.status] || "Sync will retry"}</div></PanelSectionRow>
        {game.status === "conflict" && <>
          <PanelSectionRow><div>Both devices have different progress. Both copies are kept.</div></PanelSectionRow>
          <PanelSectionRow><ButtonItem disabled={busy} onClick={() => perform(() => resolve(game.game_id, "local"))}>Use this device’s progress</ButtonItem></PanelSectionRow>
          <PanelSectionRow><ButtonItem disabled={busy} onClick={() => perform(() => resolve(game.game_id, "cloud"))}>Use cloud progress</ButtonItem></PanelSectionRow>
        </>}
        <PanelSectionRow><ButtonItem disabled={busy || game.status === "playing"} onClick={() => perform(async () => {
          setRevisions({ ...revisions, [game.game_id]: await history(game.game_id) });
        })}>Save history</ButtonItem></PanelSectionRow>
        {revisions[game.game_id] && <>
          <PanelSectionRow><DropdownItem label="Previous save" selectedOption={selected[game.game_id]}
            rgOptions={revisions[game.game_id].map(item => ({data: item.revision, label: new Date(item.created).toLocaleString()}))}
            onChange={item => { setSelected({...selected, [game.game_id]:String(item.data)}); setConfirm(""); }} /></PanelSectionRow>
          <PanelSectionRow><ButtonItem disabled={busy || !selected[game.game_id] || game.status === "playing"} onClick={() => {
            if (confirm !== game.game_id) { setConfirm(game.game_id); return; }
            void perform(async () => { await rollback(game.game_id, selected[game.game_id]); setConfirm(""); });
          }}>{confirm === game.game_id ? "Confirm restore · current save is backed up" : "Restore previous save"}</ButtonItem></PanelSectionRow>
        </>}
        <PanelSectionRow><ButtonItem disabled={busy || game.status === "playing"} onClick={() => {
          const key = "pause:" + game.game_id;
          if (confirm !== key) { setConfirm(key); return; }
          void perform(async () => {
            await setOptions(Number(game.shortcut_id), game.original_options);
            await disable(game.game_id); setConfirm("");
            toaster.toast({title:"Cloud saves paused",body:"Your local and cloud progress is kept."});
          });
        }}>{confirm === "pause:" + game.game_id ? "Confirm pause" : "Pause this game"}</ButtonItem></PanelSectionRow>
      </PanelSection>)}
    </>}
  </>;
}
export default definePlugin(() => {
  routerHook.addGlobalComponent("NoSteamSavesAutomation", Automation);
  return {name:"NoSteam Saves",titleView:<div>NoSteam Saves</div>,content:<Content/>,icon:<FaCloud/>,
    onDismount() { routerHook.removeGlobalComponent("NoSteamSavesAutomation"); }};
});
