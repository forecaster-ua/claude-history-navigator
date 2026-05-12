module.exports = {
  apps: [{
    name: 'claude-history',
    script: '/home/alexross/claude-history/venv/bin/uvicorn',
    args: 'server:app --host 127.0.0.1 --port 8055 --workers 1',
    cwd: '/home/alexross/claude-history',
    interpreter: 'none',
    env: {
      PYTHONPATH: '/home/alexross/claude-history'
    },
    error_file: '/home/alexross/claude-history/logs/err.log',
    out_file: '/home/alexross/claude-history/logs/out.log',
    log_date_format: 'YYYY-MM-DD HH:mm:ss'
  }]
};
