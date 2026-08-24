// OpenCode memory plugin for agent-m3m
const { execSync } = require('child_process');

module.exports = async function agentMemoryPlugin(context) {
  return {
    'session:start': async () => {
      try {
        const output = execSync('agent-m3m context', { encoding: 'utf-8' });
        return { context: output };
      } catch (err) {
        console.error('agent-m3m context failed', err);
      }
    },
    'session:stop': async () => {
      try {
        execSync('agent-m3m save --stdin', { input: JSON.stringify(context), encoding: 'utf-8' });
      } catch (err) {
        console.error('agent-m3m save failed', err);
      }
    }
  };
};
