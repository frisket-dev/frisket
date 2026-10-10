window.updateDownloadProgress = ({ percent }) => {
  document.getElementById('progress').value = percent;
  document.getElementById('percent').textContent = `${Math.floor(percent)}%`;
  document.getElementById('status').textContent = percent >= 100 ? 'Finishing download…' : 'Downloading…';
};
