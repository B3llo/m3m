// OpenCode memory plugin for agent-memory
const { execSync } = require('child_process');

module.exports = async function agentMemoryPlugin(context) {
  return {
    'session:start': async () => {
      try {
        const output = execSync('agent-memory context', { encoding: 'utf-8' });
        return { context: output };
      } catch (err) {
        console.error('agent-memory context failed', err);
      }
    },
    'session:stop': async () => {
      try {
        execSync('agent-memory save --stdin', { input: JSON.stringify(context), encoding: 'utf-8' });
      } catch (err) {
        console.error('agent-memory save failed', err);
      }
    }
  };
};
