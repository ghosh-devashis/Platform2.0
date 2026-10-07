// Copies the agent template's CLAUDE.md (single source of truth in the SDK) into resources/standards.md.
import { copyFileSync } from 'node:fs';
copyFileSync(new URL('../../sdk/src/ent_agent_sdk/templates/agent/CLAUDE.md', import.meta.url), new URL('../resources/standards.md', import.meta.url));
console.log('standards.md updated');
