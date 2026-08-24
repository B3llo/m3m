// OpenCode memory plugin for m3m
const { execSync } = require('child_process');

module.exports = async function agentMemoryPlugin(context) {
  return {
    'session:start': async () => {
      try {
        const output = execSync('m3m context', { encoding: 'utf-8' });
        return { context: output };
      } catch (err) {
        console.error('m3m context failed', err);
      }
    },
    'session:stop': async () => {
      try {
        execSync('m3m save --stdin', { input: JSON.stringify(context), encoding: 'utf-8' });
      } catch (err) {
        console.error('m3m save failed', err);
      }
    }
  };
};
