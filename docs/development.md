# Development

## Fake session-bus workflow

The service can run on the session bus with `FakeHardware` for
development without root:

```bash
# Terminal 1: start the service on the session bus
python -m honor_control.backend.service --session-bus

# Terminal 2: run the CLI against the session bus
honorctl --bus session status

# Terminal 3: run the GUI against the session bus
honor-control-gui --bus session
```

Session-bus mode uses `FakeHardware` and refuses to start with real
hardware writes.

## Test commands

```bash
# Run all non-hardware tests
pytest -m "not hardware"

# Run a specific test layer
pytest tests/test_core.py          # domain validation
pytest tests/test_config_store.py  # state store
pytest tests/test_hardware.py      # hardware adapter
pytest tests/test_backend.py       # snapshot/queue/supervisor
pytest tests/test_application.py   # application service
pytest tests/test_client.py        # client/codec
pytest tests/test_validation.py     # contract consistency

# Run lint
ruff check honor_control tests

# Compile check
python -m compileall honor_control
```

## Full gate

Run before committing:

```bash
ruff check honor_control tests
python -m compileall honor_control
pytest -q -m "not hardware"
QT_QPA_PLATFORM=offscreen python -c "from honor_control.frontend.gui.app import main; print('GUI OK')"
```

## Hardware test procedure

Hardware tests are **not** run in CI. They require explicit opt-in:

```bash
# Run hardware tests (requires real Honor hardware + root)
pytest -m hardware
```

Hardware tests:
- Start from stock mode.
- Apply one conservative curve.
- Observe bounded writes for a fixed interval.
- Simulate controller stop.
- Verify stock restore.
- Record measured/target values.

Never run hardware tests on unsupported machines.
