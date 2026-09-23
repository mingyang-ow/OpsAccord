# WSL development

Use a checkout on the Linux filesystem, for example under ~/projects/OpsAccord, rather than under /mnt/c. Reuse an existing checkout only after checking its remote and uncommitted work. Keep credentials in the user's existing local authentication and environment, never repository files.

The foundation targets Python 3.12 or later. Phase 1 should use uv, produce and commit uv.lock, and add the runtime and development dependencies needed for the actual implementation. Introduce PostgreSQL and the pinned Astronomy Shop deployment with separate, scoped lifecycle commands.

The current starter has no running API, UI, database, or graph. Do not present package importability as implementation completion.

## Independent review

The project includes a GPT-6 Luna reviewer definition at .codex/agents/luna_reviewer.toml. Its availability depends on the installed Codex version, authentication, and provider routing. At the start, verify the installed schema and that the reviewer resolves to gpt-6-luna on OpenAI rather than inheriting a DeepSeek endpoint. Do not change machine-wide credentials or the primary provider without authorization. If mixed-provider native subagents are unsupported, use an independently authenticated OpenAI Codex reviewer process against the same repository, and state that fallback clearly. Never substitute the primary model and label it Luna.

Official references:
- https://learn.chatgpt.com/docs/agent-configuration/subagents
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://developers.openai.com/api/docs/models/gpt-6-luna
