# @reviewsensei/cli-linux-arm64-gnu

ReviewSensei is a provider-neutral AI code review engine that turns unified
Git diffs into validated summaries and findings. This package contains the
standalone Python engine bundled for Linux arm64 with glibc 2.36+.

Install the public launcher, which selects the matching native package:

<!-- release-installation:package-npm-1:start -->
```console
npm install --save-dev @reviewsensei/cli@0.6.18
npx review-sensei --help
```
<!-- release-installation:package-npm-1:end -->

Use [`@reviewsensei/cli`](https://www.npmjs.com/package/@reviewsensei/cli) for
installation, provider/model configuration, authentication, and review examples.
The launcher forwards CLI arguments unchanged and inherits your working directory
and environment; the engine reads `.reviewsensei.yml` or an explicit `--config`.
There is no separate platform-package configuration, npm command, installer, or
download hook. A separate Python installation is unnecessary.
