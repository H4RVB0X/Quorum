# Quorum — Claude Code Instructions

## Read First
Before doing anything, read PROJECT_MEMORY.md in full. It contains the complete history of what has been built, all architecture decisions, gotchas, and current system state.

## At the End of Every Session
Append a new session block to PROJECT_MEMORY.md following the exact same format as existing entries:

Be specific. Future Claude sessions rely entirely on this document for context. Do not skip this step.

## Key Facts
- Graph ID: d3a38be8-37d9-4818-be28-5d2d0efa82c0
- Root .env uses localhost hostnames (for host Python scripts)
- backend/.env uses neo4j/ollama hostnames (for Docker containers)
- Dashboard served at localhost:5001/dashboard
- Project is called Quorum in docs, mirofish in internal code
- After any backend change: docker cp the file + docker restart mirofish-offline