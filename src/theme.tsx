import type { CSSProperties } from "react";
import { FaCloud } from "react-icons/fa";

export const pluginStyles = `
.nss-root{color:#edf5ff;padding-bottom:12px}
.nss-hero{margin:8px 0 12px;padding:14px 16px;background:#1b4263;border-radius:8px}
.nss-brand{display:flex;align-items:center;gap:10px;font-size:19px;font-weight:700;color:#e6f4ff;margin-bottom:12px}
.nss-brand svg{color:#8bd2ff;flex:none}
.nss-hero p{font-size:13px;line-height:1.5;margin:0;color:#c6deef}
.nss-block{margin:0 0 12px;padding:8px 0 10px;background:#152b40;border:1px solid #32506b;border-radius:8px}
.nss-account{background:#153840;border-color:#37636a}
.nss-overview{background:#102f37;border-color:#2d5e65}
.nss-game{background:#172b40}
.nss-game-conflict{background:#332c20;border-color:#8d7043}
.nss-root .nss-copy{font-size:13px;line-height:1.55;color:#bfd0e2}
.nss-root .nss-alert{padding:12px;background:#3b292e;color:#ffc1c8;border-radius:6px;font-size:13px;line-height:1.5}
.nss-status{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;line-height:1.5;padding:9px 11px;border-radius:5px}
.nss-status-dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
.nss-root button{max-width:100%;box-sizing:border-box;white-space:normal}
.nss-totals{display:flex;gap:18px;margin:8px 0 0;font-size:12px;color:#bfd0e2}
.nss-totals strong{font-size:20px;color:#edf5ff;display:block;margin-bottom:2px}
.nss-pending{padding:12px;background:#102235;border-radius:6px;width:100%;box-sizing:border-box}
.nss-pending strong{display:block;color:#e3f0ff;margin-bottom:5px;font-size:14px}
.nss-pending span{color:#a6c7e2;font-size:12px;line-height:1.5;display:block}
`;
export function PluginTheme() { return <style>{pluginStyles}</style>; }
export function CloudIntro({ games = 0, pending = 0, connected = false }: { games?: number; pending?: number; connected?: boolean }) {
  return <div className="nss-hero">
    <div className="nss-brand"><FaCloud aria-hidden="true"/>NoSteam Saves</div>
    <p>Your non-Steam progress, ready for your next session.</p>
    {connected && <div className="nss-totals">{games} games connected · {pending} finding saves</div>}
  </div>;
}
export function SaveStatus({ status, label }: { status: string; label: string }) {
  const healthy = ["synced", "connected", "recovered"].includes(status);
  const attention = ["conflict", "offline", "pending", "error", "interrupted"].includes(status);
  const style: CSSProperties = healthy ? {background:"#103a36",color:"#91e8cd"}
    : attention ? {background:"#40341e",color:"#ffda94"}
    : {background:"#1c3e5b",color:"#a9d9ff"};
  return <div className="nss-status" style={style} aria-live="polite"><span className="nss-status-dot" aria-hidden="true"/>{label}</div>;
}
