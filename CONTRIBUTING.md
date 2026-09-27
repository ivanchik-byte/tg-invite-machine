# Contributing to tg-invite-machine

Thank you for your interest in contributing to tg-invite-machine. Contributions ranging from bug reports and fixes to documentation improvements and test coverage are welcome.

Before starting, please review this guide to keep collaboration clean, fast, and structured.

## Contact and Reporting Issues

If you run into a bug, unexpected error, or have questions about setup and usage, you can reach out directly:

* **Telegram PM:** [https://t.me/ivanchikbyte](https://t.me/ivanchikbyte) (direct maintainer contact for quick troubleshooting and bug reports)
* **Telegram Channel:** [https://t.me/ivanchik_byte](https://t.me/ivanchik_byte) (project announcements and release updates)
* **GitHub Issues:** [Open a bug report or feature request](https://github.com/ivanchik-byte/tg-invite-machine/issues)

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md). Please report any unacceptable behavior directly to the maintainer via Telegram: [@ivanchikbyte](https://t.me/ivanchikbyte).

## Local Development Setup

### Prerequisites

* Python 3.11 or higher
* Git
* SQLite 3

### Environment Setup

1. Fork the repository on GitHub and clone your fork:

   ```bash
   git clone https://github.com/<your-username>/tg-invite-machine.git
   cd tg-invite-machine
   ```

2. Create and activate a Python virtual environment:

   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install project dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Create your local `.env` configuration file:

   ```bash
   cp .env.example .env
   ```

   Fill in your development values:
   * `BOT_TOKEN`: Test bot token from [@BotFather](https://t.me/BotFather)
   * `ADMIN_ID`: Your numeric Telegram user ID
   * `ENCRYPTION_KEY`: A Fernet URL-safe 32-byte key (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)

## Development Workflow

### Branch Naming Conventions

Create a dedicated feature branch from `main` using standard prefixes:

* `feat/feature-name`: New features or enhancements
* `fix/bug-description`: Bug fixes and crash corrections
* `docs/topic-name`: Documentation updates
* `refactor/scope`: Structural changes without changing external behavior
* `test/test-description`: Adding or improving tests

Example:

```bash
git checkout -b feat/per-session-delay
```

### Running Tests

Always run the automated test suite before opening a pull request:

```bash
pytest -W error
```

To run a specific test module or test function:

```bash
pytest tests/test_security.py
pytest tests/test_inviter.py -k test_custom_speed_profile
```

All tests must pass cleanly with zero warnings or errors.

### Code Quality and Style Guidelines

* Follow PEP 8 and standard Python idioms.
* Keep functions modular and maintain strict type annotations on all public functions.
* Never commit hardcoded API keys, tokens, session strings, or `.env` files.
* Preserve strict security boundaries: validate proxy credentials and encrypt session strings at rest.
* Keep documentation clear, concise, and professional: no emojis and no em dashes.

### Commit Message Conventions

We follow the Conventional Commits specification:

```text
<type>(<scope>): <short description in imperative mood>
```

Common types:

* `feat`: A new feature
* `fix`: A bug fix
* `refactor`: Code restructuring with no behavior change
* `docs`: Documentation updates
* `test`: Adding or correcting tests
* `chore`: Dependencies, tooling, or build configuration

Examples:

* `feat(inviter): support dynamic jitter per account`
* `fix(bot): handle empty user profile gracefully`
* `docs(readme): clarify local installation steps`

## Submitting a Pull Request

1. Push your branch to your GitHub fork:

   ```bash
   git push origin feat/your-feature-name
   ```

2. Open a Pull Request against the `main` branch of `ivanchik-byte/tg-invite-machine`.
3. Fill out the pull request template describing the problem solved and testing performed.
4. Verify that all automated tests pass.
5. Address code review feedback promptly.
