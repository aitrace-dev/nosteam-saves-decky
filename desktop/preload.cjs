const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('nosteam', {
  status: () => ipcRenderer.invoke('nosteam:status'),
  artwork: (key, kind) => ipcRenderer.invoke('nosteam:artwork', key, kind),
  submit: (action, args = {}) => ipcRenderer.invoke('nosteam:submit', action, args),
  task: id => ipcRenderer.invoke('nosteam:task', id),
  openSignIn: () => ipcRenderer.invoke('nosteam:sign-in'),
  setupSaves: key => ipcRenderer.invoke('nosteam:setup-saves', key),
  shareGame: key => ipcRenderer.invoke('nosteam:share-game', key),
  downloadGame: (game, version) => ipcRenderer.invoke('nosteam:download-game', game, version),
  openInstallation: id => ipcRenderer.invoke('nosteam:open-installation', id),
});
