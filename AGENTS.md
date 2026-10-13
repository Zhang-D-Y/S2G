# S2G Workspace Conventions

- Maintain project plans, specifications, architecture, and interface documentation in English under `S2G/docs/`.
- Maintain experimental data documentation, integration records, smoke reports, and case studies in English under `S2G/data/`.
- Do not create module-local documentation trees such as `spec2gds/docs/` or `docs/datas/`. Component READMEs may retain concise usage instructions and link to the canonical directories.
- Keep both `docs/` and `data/`, along with experimental run/work artifacts, excluded from Git under the source-only publication policy.
- Keep project runtime scripts and script dependencies inside S2G; external EDA toolchain invocations and process assets are allowed.
- When reorganizing documents, preserve filenames, code, identifiers, numeric results, and evidence. Update relative links; preserve the original locations and hashes of frozen raw experiment artifacts.
