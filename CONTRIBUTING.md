# Contributing

## Local workflow

1. Create a feature branch:
   - git checkout -b feat/short-description

2. Make small commits with clear messages:
   - git add <files>
   - git commit -m "feat: add X; fix Y"

3. Run tests and smoke test locally:
   - pytest (if tests exist)
   - pwsh .\scripts\smoke_test.ps1 -Port 3000 -TimeoutSeconds 15

4. Merge into main after local verification:
   - git checkout main
   - git merge --no-ff feat/short-description

## Branch naming
- main — stable branch
- eat/* — new features
- ix/* — bug fixes
- chore/* — maintenance and tooling

## Pull requests
When you push to a remote and open a PR, include:
- What changed
- How to test locally
- Any breaking changes
