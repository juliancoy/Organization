# Workspace instructions

## Testing

- Use host-based tests or the local Docker deployment by default.
- Do not use a virtual machine (VM) for testing unless the user explicitly requests it.
- Log every test run alongside the code it tests. Record the date, command, result, and the tested code revision or content fingerprint so previous results can be matched to the code.
- Do not rerun tests for unchanged code. Consult the adjacent test log and reuse recorded results when the tested code and relevant dependencies, configuration, and test inputs are unchanged. Rerun only the tests affected by changes.
