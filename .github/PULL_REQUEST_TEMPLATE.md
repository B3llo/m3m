## Summary

Describe the problem and the change.

## Validation

- [ ] Tests added or updated where behavior changed
- [ ] `python -m unittest discover -s tests -v` passes
- [ ] `ruff check src tests` passes
- [ ] Documentation reflects user-visible changes

## Safety and compatibility

- [ ] Existing memory files remain readable
- [ ] No private transcripts, memories, credentials, or generated `.agent-m3m` data are included
- [ ] Hook changes were tested against the affected agent integration

## Release note

State the user-visible impact, or write `none`.
