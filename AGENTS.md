# Project workflow

For every modification to this repository, use this release order without exception:

1. Make the change and run the relevant tests.
2. Commit the completed change to Git.
3. Push the commit to `origin`.
4. Only after the push succeeds, deploy or restart the production service.
5. Verify the live service after deployment.

Never deploy or restart production with uncommitted or unpushed changes.
