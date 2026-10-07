# @reviewsensei/cli-darwin-x64

ReviewSensei is a provider-neutral AI code review engine that turns unified
Git diffs into validated summaries and findings. This package contains the
standalone Python engine bundled for macOS Intel.

Install the public launcher, which selects the matching native package:

```console
npm install --save-dev @reviewsensei/cli@0.6.17
npx review-sensei --help
```

Use [`@reviewsensei/cli`](https://www.npmjs.com/package/@reviewsensei/cli) for
installation, provider/model configuration, authentication, and review examples.
The launcher forwards CLI arguments unchanged and inherits your working directory
and environment; the engine reads `.reviewsensei.yml` or an explicit `--config`.
There is no separate platform-package configuration, npm command, installer, or
download hook. A separate Python installation is unnecessary.
