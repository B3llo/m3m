# Security policy

agent-m3m processes local transcripts, memory files, configuration, and optional provider credentials. Please avoid including any of that private data in public issues, pull requests, logs, or test fixtures.

## Supported versions

Until the first stable release, only the latest `0.3.x` release is eligible for security fixes. Older development snapshots are unsupported. This policy will be revised when multiple release lines are maintained.

## Reporting a vulnerability

Use the repository's **Security** tab and select **Report a vulnerability** to create a private GitHub security advisory. Do not open a public issue for suspected vulnerabilities.

Include a minimal reproduction, affected versions, impact, and any suggested mitigation. Remove API keys, real transcripts, memory content, usernames, paths, and other personal data.

Maintainers aim to acknowledge reports within 7 days and provide an initial assessment within 14 days. Timelines for a fix and disclosure depend on severity and complexity. Please allow time for a coordinated release before public disclosure.

## Scope

Security issues include unintended disclosure or corruption of memory data, command or hook injection, unsafe file permissions, authentication secret exposure, and bypasses of documented approval or archival safeguards. General bugs and feature requests belong in the public issue tracker after sensitive data is removed.
