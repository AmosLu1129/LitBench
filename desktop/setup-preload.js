// SPDX-License-Identifier: AGPL-3.0-or-later
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('litbenchSetup', {
  getServerUrl: () => ipcRenderer.invoke('litbench:get-server-url'),
  saveServerUrl: (url) => ipcRenderer.invoke('litbench:save-server-url', url),
  cancel: () => ipcRenderer.invoke('litbench:cancel-setup')
})
