// 1012v2 抓拍窗专用 preload：只暴露 send/close 两个口子（最小权限面）
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('camApi', {
  send: (d) => ipcRenderer.send('cam:result', d),   // 1012v2fix: 通道名必须与主进程 ipcMain.on('cam:result') 一致
  close: () => ipcRenderer.send('cam:close'),
});
