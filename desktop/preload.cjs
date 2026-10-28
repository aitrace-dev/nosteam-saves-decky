const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('nosteam', {
  status: () => ipcRenderer.invoke('nosteam:status'),
  artwork: (key, kind) => ipcRenderer.invoke('nosteam:artwork', key, kind),
  submit: (action, args = {}) => ipcRenderer.invoke('nosteam:submit', action, args),
  task: id => ipcRenderer.invoke('nosteam:task', id),
  openSignIn: () => ipcRenderer.invoke('nosteam:sign-in'),
});
