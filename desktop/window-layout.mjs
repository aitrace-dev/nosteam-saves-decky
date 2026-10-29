export function gamingSession(env = process.env, gamescopeProperty = '') {
  return /gamescope/i.test([env.XDG_CURRENT_DESKTOP, env.XDG_SESSION_DESKTOP, env.DESKTOP_SESSION].join(' '))
    || !!(Number(env.SteamGameId) || Number(env.SteamAppId))
    || /GAMESCOPE_PID\(CARDINAL\) = \d+/.test(gamescopeProperty);
}

export function consoleZoom(width, height) {
  return Math.max(1, Math.min(width / 1280, height / 800));
}
