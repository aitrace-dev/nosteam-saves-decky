import { useEffect, useState } from "react";
import { callable, definePlugin, toaster } from "@decky/api";
import {
  ButtonItem, DropdownItem, PanelSection, PanelSectionRow, TextField,
} from "@decky/ui";
import { FaCloud } from "react-icons/fa";

type Shortcut = {
  shortcut_id: string; title: string; registry: string; launch_options: string;
  suggested_roots?: string[]; matched_title?: string;
};
type Game = Shortcut & { game_id: string; status: string; original_options: string };
type Status = { connected: boolean; server: string; games: Game[] };
type CloudGame = { game: string; title: string };
type Revision = { revision: string; created: string; device: string };

// This deliberately uses the shortcut-specific setter, never SetAppLaunchOptions.
declare const SteamClient: {
  Apps: {
    SetShortcutLaunchOptions(id: number, options: string): void;
    RegisterForAppDetails(id: number, callback: (details: {
      strShortcutLaunchOptions: string;
    }) => void): { unregister(): void };
  };
};

function currentOptions(id: number): Promise<string> {
  return new Promise((accept, reject) => {
    let registration: { unregister(): void } | undefined;
    const timer = setTimeout(() => {
      registration?.unregister();
      reject(new Error("Steam did not provide current shortcut settings. Try again."));
    }, 5000);
    registration = SteamClient.Apps.RegisterForAppDetails(id, (details) => {
      if (typeof details.strShortcutLaunchOptions !== "string") return;
      clearTimeout(timer);
      // Defer cleanup so synchronous callbacks also unregister correctly.
      queueMicrotask(() => registration?.unregister());
      accept(details.strShortcutLaunchOptions);
    });
  });
}

async function setOptions(id: number, options: string) {
  SteamClient.Apps.SetShortcutLaunchOptions(id, options);
  // The Steam setter returns void; verify it through the details subscription.
  for (let attempt = 0; attempt < 5; attempt++) {
    if (await currentOptions(id) === options) return;
    await new Promise((accept) => setTimeout(accept, 200));
  }
  throw new Error("Steam did not save the launch settings. Try again.");
}

const getStatus = callable<[], Status>("get_status");
const discover = callable<[], Shortcut[]>("discover");
const cloudGames = callable<[], CloudGame[]>("cloud_games");
const updateDefinitions = callable<[], { games: number }>("update_definitions");
const findSaves = callable<[string, string], {
  status: string; suggested_roots: string[]; message: string;
}>("find_saves");
const connect = callable<[string, string], unknown>("connect");
const enable = callable<[string, string, string, string[], string], {
  launch_options: string; original_options: string;
}>("enable");
const disable = callable<[string], unknown>("disable");
const sync = callable<[string], unknown>("sync");
const resolve = callable<[string, string], unknown>("resolve");
const history = callable<[string], Revision[]>("history");
const rollback = callable<[string, string], unknown>("rollback");

const labels: Record<string, string> = {
  ready: "Ready to sync", checking: "Checking saves…", downloading: "Downloading saves…",
  uploading: "Uploading saves…", synced: "Up to date", playing: "In game",
  pending: "Upload pending", offline: "Offline", conflict: "Choose a save",
  saving: "Saving…", recovering: "Recovering saves…", recovered: "Saves recovered",
  interrupted: "Last session interrupted", error: "Needs attention",
};

function Content() {
  const [status, setStatus] = useState<Status>();
  const [shortcuts, setShortcuts] = useState<Shortcut[]>([]);
  const [cloud, setCloud] = useState<CloudGame[]>([]);
  const [selection, setSelection] = useState("");
  const [cloudId, setCloudId] = useState("");
  const [savePaths, setSavePaths] = useState<string[]>([""]);
  const [server, setServer] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revisions, setRevisions] = useState<Record<string, Revision[]>>({});
  const [restoreId, setRestoreId] = useState<Record<string, string>>({});
  const [confirmRestore, setConfirmRestore] = useState<string>();
  const [confirmDisable, setConfirmDisable] = useState<string>();
  const [searchNote, setSearchNote] = useState("");
  const [searchCandidates, setSearchCandidates] = useState<string[]>([]);

  async function refresh() {
    const next = await getStatus();
    setStatus(next);
    setShortcuts(await discover());
    if (next.connected) setCloud(await cloudGames());
  }

  async function perform(action: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await action();
      await refresh();
    } catch (failure) {
      const message = failure instanceof Error ? failure.message : "Could not finish this action";
      setError(message);
      toaster.toast({ title: "NoSteam Saves", body: message });
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    refresh().catch((failure: unknown) => {
      if (active) setError(String(failure));
    });
    const timer = setInterval(() => {
      getStatus().then((next) => { if (active) setStatus(next); }).catch(() => {});
    }, 3000);
    return () => { active = false; clearInterval(timer); };
  }, []);

  const available = shortcuts.filter((item) => !status?.games.some(
    (game) => game.shortcut_id === item.shortcut_id && game.registry === item.registry,
  ));
  const selected = available.find((item) => `${item.registry}:${item.shortcut_id}` === selection);

  async function findSavesFor(shortcut: Shortcut) {
    setSearchNote("Finding save folders…");
    const result = await findSaves(shortcut.shortcut_id, shortcut.registry);
    setSearchNote(result.message);
    setSearchCandidates(result.suggested_roots);
    setSavePaths(result.suggested_roots.length === 1 ? result.suggested_roots : [""]);
  }

  async function enableSelected() {
    if (!selected) return;
    const id = cloudId || crypto.randomUUID();
    const appId = Number(selected.shortcut_id);
    const original = await currentOptions(appId);
    const result = await enable(selected.shortcut_id, selected.registry, id, savePaths, original);
    try {
      await setOptions(appId, result.launch_options);
    } catch (failure) {
      // Keep the binding if rollback fails, so an installed wrapper stays usable.
      await setOptions(appId, result.original_options);
      await disable(id);
      throw failure;
    }
    setSelection(""); setSavePaths([""]); setCloudId("");
    toaster.toast({ title: "Cloud saves enabled", body: "Use Play as usual. Saves sync automatically." });
  }

  return <>
    <PanelSection title="NoSteam Saves">
      <PanelSectionRow>
        <div>Cloud saves for non-Steam games. Play normally; saves sync before launch and after exit.</div>
      </PanelSectionRow>
      {error && <PanelSectionRow><div role="alert">{error}</div></PanelSectionRow>}
      <PanelSectionRow>
        <ButtonItem disabled={busy} onClick={() => perform(refresh)}>Refresh</ButtonItem>
      </PanelSectionRow>
    </PanelSection>
    {!status?.connected ? <PanelSection title="Connect your server">
      <PanelSectionRow><TextField label="Server address" value={server}
        placeholder="https://saves.example.com" onChange={(event) => setServer(event.target.value)} />
      </PanelSectionRow>
      <PanelSectionRow><TextField label="Server key" bIsPassword value={token}
        onChange={(event) => setToken(event.target.value)} /></PanelSectionRow>
      <PanelSectionRow><ButtonItem disabled={busy || !server || !token}
        onClick={() => perform(async () => {
          await connect(server, token); setToken("");
          try { await updateDefinitions(); } catch {
            toaster.toast({ title: "Server connected", body: "Game definitions unavailable. You can still choose save folders." });
          }
        })}>
        Connect
      </ButtonItem></PanelSectionRow>
    </PanelSection> : <>
      <PanelSection title="Your games">
        {!status.games.length && <PanelSectionRow><div>Enable your first game below.</div></PanelSectionRow>}
        {status.games.map((game) => <PanelSection key={game.game_id} title={game.title}>
          <PanelSectionRow><div aria-live="polite">{labels[game.status] || "Needs attention"}</div></PanelSectionRow>
          {game.status === "conflict" ? <>
            <PanelSectionRow><div>Both devices changed this save. Both copies are backed up.</div></PanelSectionRow>
            <PanelSectionRow><ButtonItem disabled={busy}
              onClick={() => perform(() => resolve(game.game_id, "local"))}>Use this device</ButtonItem></PanelSectionRow>
            <PanelSectionRow><ButtonItem disabled={busy}
              onClick={() => perform(() => resolve(game.game_id, "cloud"))}>Use cloud save</ButtonItem></PanelSectionRow>
          </> : <PanelSectionRow><ButtonItem disabled={busy || game.status === "playing"}
            onClick={() => perform(() => sync(game.game_id))}>Sync now</ButtonItem></PanelSectionRow>}
          <PanelSectionRow><ButtonItem disabled={busy || game.status === "playing"}
            onClick={() => perform(async () => {
              setRevisions({ ...revisions, [game.game_id]: await history(game.game_id) });
            })}>Save history</ButtonItem></PanelSectionRow>
          {revisions[game.game_id] && <>
            <PanelSectionRow><DropdownItem label="Restore a save"
              selectedOption={restoreId[game.game_id]}
              rgOptions={revisions[game.game_id].map((item) => ({
                data: item.revision,
                label: `${new Date(item.created).toLocaleString()} · ${item.device}`,
              }))}
              onChange={(item) => {
                setRestoreId({ ...restoreId, [game.game_id]: String(item.data) });
                setConfirmRestore(undefined);
              }} /></PanelSectionRow>
            <PanelSectionRow><ButtonItem
              disabled={busy || !restoreId[game.game_id] || game.status === "playing"}
              onClick={() => {
                if (confirmRestore !== game.game_id) { setConfirmRestore(game.game_id); return; }
                void perform(async () => {
                  await rollback(game.game_id, restoreId[game.game_id]);
                  setConfirmRestore(undefined);
                });
              }}>{confirmRestore === game.game_id ? "Confirm restore (current save is backed up)" : "Restore selected save"}
            </ButtonItem></PanelSectionRow>
          </>}
          <PanelSectionRow><ButtonItem disabled={busy || game.status === "playing"}
            onClick={() => {
              if (confirmDisable !== game.game_id) { setConfirmDisable(game.game_id); return; }
              void perform(async () => {
                await setOptions(Number(game.shortcut_id), game.original_options);
                await disable(game.game_id);
                setConfirmDisable(undefined);
              });
            }}>{confirmDisable === game.game_id ? "Confirm disable (saves are kept)" : "Disable cloud saves"}
          </ButtonItem></PanelSectionRow>
        </PanelSection>)}
      </PanelSection>
      <PanelSection title="Enable cloud saves">
        <PanelSectionRow><ButtonItem disabled={busy}
          onClick={() => perform(() => updateDefinitions())}>Update game definitions</ButtonItem></PanelSectionRow>
        <PanelSectionRow><DropdownItem label="Non-Steam game" strDefaultLabel="Choose a game"
          disabled={busy}
          selectedOption={selection}
          rgOptions={available.map((item) => ({
            data: `${item.registry}:${item.shortcut_id}`, label: item.title,
          }))}
          onChange={(item) => {
            const value = String(item.data);
            setSelection(value);
            const shortcut = available.find((entry) => `${entry.registry}:${entry.shortcut_id}` === value);
            const matches = cloud.filter((entry) => entry.title === shortcut?.title);
            setCloudId(matches.length === 1 ? matches[0].game : "");
            setSavePaths(shortcut?.suggested_roots?.length ? shortcut.suggested_roots : [""]);
            setSearchNote(""); setSearchCandidates([]);
            if (shortcut && !shortcut.suggested_roots?.length) {
              void perform(() => findSavesFor(shortcut));
            }
          }} /></PanelSectionRow>
        {selected && <>
          <PanelSectionRow><ButtonItem disabled={busy}
            description="Checks folders with your server. Folder names and file metadata may be sent to Gemini."
            onClick={() => perform(() => findSavesFor(selected))}>
            Find saves automatically
          </ButtonItem></PanelSectionRow>
          {searchNote && <PanelSectionRow><div aria-live="polite">{searchNote}</div></PanelSectionRow>}
          {searchCandidates.length > 1 && <PanelSectionRow><DropdownItem
            label="Choose a potential save folder" strDefaultLabel="Review the matches"
            selectedOption={savePaths.length === 1 ? savePaths[0] : ""}
            rgOptions={searchCandidates.map((path) => ({ data: path, label: path }))}
            onChange={(item) => setSavePaths([String(item.data)])} />
          </PanelSectionRow>}
          <PanelSectionRow><DropdownItem label="Cloud save" selectedOption={cloudId}
            rgOptions={[{ data: "", label: "Start a new cloud save" },
              ...cloud.map((item) => ({ data: item.game, label: item.title }))]}
            onChange={(item) => setCloudId(String(item.data))} /></PanelSectionRow>
          {selected.suggested_roots?.length ? <PanelSectionRow><div>
            Save folders found. Check these belong to the game before enabling.
          </div></PanelSectionRow> : null}
          {savePaths.map((path, index) => <PanelSectionRow key={index}>
            <TextField label={savePaths.length > 1 ? `Save folder ${index + 1}` : "Save folder"}
              value={path} description="Use the same folder order on both devices."
              onChange={(event) => setSavePaths(savePaths.map((value, position) =>
                position === index ? event.target.value : value))} />
          </PanelSectionRow>)}
          <PanelSectionRow><ButtonItem disabled={busy || savePaths.length >= 8}
            onClick={() => setSavePaths([...savePaths, ""])}>Add another save folder</ButtonItem></PanelSectionRow>
          {savePaths.length > 1 && <PanelSectionRow><ButtonItem disabled={busy}
            onClick={() => setSavePaths(savePaths.slice(0, -1))}>Remove last folder</ButtonItem></PanelSectionRow>}
          <PanelSectionRow><ButtonItem disabled={busy || savePaths.some((path) => !path.trim())}
            onClick={() => perform(enableSelected)}>Enable cloud saves</ButtonItem></PanelSectionRow>
        </>}
        {!available.length && <PanelSectionRow><div>No other non-Steam shortcuts found.</div></PanelSectionRow>}
      </PanelSection>
    </>}
  </>;
}

export default definePlugin(() => ({
  name: "NoSteam Saves", titleView: <div>NoSteam Saves</div>, content: <Content />,
  icon: <FaCloud />,
}));
