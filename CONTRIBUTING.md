# Contributing to Tokan

Thank you for your interest in contributing! This document provides guidelines and instructions for contributing.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Running Tests](#running-tests)
- [Code Style](#code-style)
- [Submitting Changes](#submitting-changes)
- [Adding New Agent Adapters](#adding-new-agent-adapters)
- [Reporting Bugs](#reporting-bugs)
- [Requesting Features](#requesting-features)

## Code of Conduct

Be respectful and inclusive. We want this to be a welcoming space for everyone.

## Getting Started

1. Fork the repository on GitHub
2. Clone your fork locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/Tokan.git
   cd Tokan
   ```
3. Add upstream remote:
   ```bash
   git remote add upstream https://github.com/deepusingh2530/Tokan.git
   ```

## Development Setup

1. Create a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

2. Install in editable mode with dev dependencies:
   ```bash
   pip install -e ".[dev]"
   ```

3. Verify installation:
   ```bash
   python3 -c "import tokan; print(tokan.__version__)"
   token-optimizer --help
   ```

## Running Tests

### All Tests
```bash
python3 -m unittest discover -s tests
```

### Single Test Module
```bash
python3 -m unittest tests.test_cdm
python3 -m unittest tests.test_cache
python3 -m unittest tests.test_pipeline_and_proxy
```

### With Coverage
```bash
pip install pytest pytest-cov
pytest tests/ -v --cov=tokan --cov-report=html
open htmlcov/index.html
```

### Test Categories

- **Unit tests** (`test_cdm.py`, `test_cache.py`, `test_jev.py`): Test individual modules
- **Integration tests** (`test_pipeline_and_proxy.py`, `test_anthropic_proxy.py`): Test end-to-end flows with fake upstreams
- **Adapter tests** (`test_adapters.py`): Test registry, detection, snippet rendering
- **MCP tests** (`test_mcp.py`): Test JSON-RPC dispatch and tool behavior

**All tests must pass before submitting a PR.**

## Code Style

We use:
- **Black** for formatting (line length 100)
- **isort** for import sorting
- **mypy** for type checking (strict mode)
- **ruff** for linting

### Auto-format
```bash
black tokan/
isort tokan/
mypy tokan/
ruff check tokan/ --fix
```

### Pre-commit Hooks (optional but recommended)
```bash
pip install pre-commit
pre-commit install
```

## Submitting Changes

1. Create a feature branch:
   ```bash
   git checkout -b feature/my-feature
   ```

2. Make your changes and commit with descriptive messages:
   ```bash
   git add -A
   git commit -m "Add Gemini protocol support to proxy"
   ```

3. Push to your fork:
   ```bash
   git push origin feature/my-feature
   ```

4. Open a Pull Request on GitHub with:
   - Clear title describing the change
   - Description of what changed and why
   - Link to any related issues
   - Test results (if applicable)

5. Wait for review. Address any feedback.

### Commit Message Format

Use concise, imperative tense:
- ✅ `Add Gemini protocol support`
- ✅ `Fix cache TTL enforcement`
- ✅ `Update README with benchmarks`
- ❌ `Added Gemini support` (past tense)
- ❌ `Fixed bug` (too vague)

## Adding New Agent Adapters

Tokan is designed to be extensible. Adding a new agent adapter is the most common contribution type.

### Built-in Adapter (in this repo)

1. Open `tokan/adapters/builtin.py`

2. Add your adapter class:
   ```python
   @register
   class MyAgentAdapter(AgentAdapter):
       id = "my-agent"
       display_name = "My Agent"
       protocols = ("openai", "mcp")  # Which wire protocols it speaks
       executables = ("my-agent",)    # Binary name for detection
       config_paths = ("~/.myagent",) # Config file patterns for detection
       summary = "Brief description shown in listings"
       
       def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
           return [
               self._snippet(
                   kind="env",           # "env", "file", "ui", "command"
                   target="shell",       # Where to apply
                   body=f"export MY_AGENT_LLM_URL={gw.openai_base_url}",
                   note="Optional note"
               ),
           ]
       
       def mcp_target(self) -> str:
           return "~/.myagent/mcp.json"
       
       def mcp_snippet(self, gw: GatewayInfo, *, target: str = "") -> ConfigSnippet:
           import json
           return self._snippet(
               kind="file",
               target=target or self.mcp_target(),
               body=json.dumps({
                   "mcpServers": {
                       "tokan": {
                           "command": gw.mcp_command[0],
                           "args": list(gw.mcp_command[1:])
                       }
                   }
               }, indent=2),
           )
   ```

3. Add tests in `tests/test_adapters.py`

4. Update README.md compatibility matrix

### Third-Party Adapter (in your own package)

Create a separate Python package:

```
my-tokan-adapter/
├── pyproject.toml
├── my_pkg/
│   ├── __init__.py
│   └── adapter.py
```

In `pyproject.toml`:
```toml
[project]
name = "my-tokan-adapter"
version = "0.1.0"
dependencies = ["Tokan>=0.1.0"]

[project.entry-points."tokan.adapters"]
my-agent = "my_pkg.adapter:MyAgentAdapter"
```

In `my_pkg/adapter.py`:
```python
from tokan.adapters import AgentAdapter, GatewayInfo

class MyAgentAdapter(AgentAdapter):
    # ... same as above
    pass
```

Users install your package and Tokan auto-discovers the adapter. No changes to Tokan needed.

### Adapter Testing Checklist

- [ ] `id` is unique and kebab-case (e.g., `my-agent`)
- [ ] `display_name` is human-readable
- [ ] `protocols` correctly lists wire protocols (`"openai"`, `"anthropic"`, `"gemini"`, `"mcp"`)
- [ ] `executables` lists binary names for `shutil.which()` detection
- [ ] `config_paths` lists config file patterns (supports `~`)
- [ ] `summary` is one line
- [ ] `snippets()` returns copy-pasteable config for each protocol in `protocols`
- [ ] MCP snippets use the correct config format for the agent
- [ ] Snippets include the gateway URLs from `GatewayInfo`
- [ ] Detection works: create a temp dir with config file, verify `adapter.detect()` returns a string
- [ ] `render()` output is valid and readable

## Reporting Bugs

Open an issue with:

- **Title**: Concise description (e.g., "Cache returns stale data after TTL expiry")
- **Description**:
  - What happened?
  - What did you expect?
  - Steps to reproduce
  - Environment (OS, Python version, Tokan version)
  - Logs / error messages
  - Minimal reproduction case (if possible)

## Requesting Features

Open an issue with:

- **Title**: Feature request description
- **Use case**: Why is this needed? What problem does it solve?
- **Proposed solution**: How should it work?
- **Alternatives considered**: Other approaches you've considered
- **Additional context**: Examples, references, etc.

## Good First Issues

Look for issues labeled `good first issue`. These are:
- Well-scoped
- Documented
- Non-controversial
- Good for learning the codebase

## Release Process (Maintainers Only)

1. Update `CHANGELOG.md` with release notes
2. Bump version in `pyproject.toml`
3. Create release branch: `git checkout -b release/v0.2.0`
4. Run full test suite
5. Build distribution: `python3 -m build`
6. Test locally: `pip install dist/Tokan-0.2.0-py3-none-any.whl`
7. Tag release: `git tag v0.2.0`
8. Push: `git push upstream main --tags`
9. Upload to PyPI: `twine upload dist/*`
10. Create GitHub release with changelog

## Questions?

Open a discussion or reach out on GitHub Issues. We're happy to help!

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
