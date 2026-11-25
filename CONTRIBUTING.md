# Contributing to nimbo

Open a GitHub issue for a bug or proposed feature. For a vulnerability, follow
the [security policy](SECURITY.md). For bug reports, include the nimbo version
(from `backup --version`), Python version, Linux distribution, and reproducible
steps using sample data.

## Develop and verify

1. Fork the repository and make changes on a branch.
2. Set up the development environment and run the checks in the
   [testing guide](docs/testing.md).
3. Open a pull request describing the user-facing change, the tests you ran,
   and any deliberate dashboard snapshot updates.

Keep code, comments, and developer documentation in English. Update both
Spanish and English translations for user-facing CLI or dashboard changes.
Keep the two README files and OAuth guides in sync when their content changes.
Use the fake Drive for routine testing, including Drive write and recovery
scenarios. Use synthetic paths and file names in test fixtures and examples.

When modifying the engine, consult the [architecture](docs/architecture.md)
and preserve the mirror's trash, membership, and confirmation safeguards.
The project is licensed under the [MIT license](LICENSE).
