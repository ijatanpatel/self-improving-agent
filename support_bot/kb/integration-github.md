# GitHub integration

## Connecting GitHub
An Admin installs the Orbitask GitHub app from Settings > Integrations > GitHub and chooses which repositories it can see. GitHub.com and GitHub Enterprise Cloud are supported. GitHub Enterprise Server (self-hosted) and GitLab are not supported.

## Linking commits and pull requests
Put the task ID, for example ORB-123, in a branch name, a pull request title or a commit message. The commit or pull request then shows up in the task's Development panel with its status.

## Completing tasks on merge
If "Auto-complete on merge" is turned on in the project settings, merging a linked pull request moves the task to Done. It is off by default.

## Troubleshooting
If links do not appear, check that the repository is selected in the GitHub app settings and that the task ID uses the exact prefix of the project. Links only appear for activity after the integration was installed.
