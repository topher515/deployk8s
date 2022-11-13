# Help managing k8s + helm

## Setup script

- Add `<this project>/bin` to your `$PATH`

## Setup your project

Create a subdir in your helm chart dir like `dev` or `staging-3`. Populate
it with files/directories like below:

Setup your deploy (helm) dir like:

- `helm` 
  - Chart.yaml
  - `{wiz_dir_envname_1}`
    - `wiz.yml`
    - `.env`, like:
      ---
      ENV_VAR_1=foo1
      ENV_VAR_2=bar
      ---
    - `secretfiles`
      - `var`
        - `google_credential.json`
        - `anotherfile.txt`
  - `{wiz_dir_envname_2}`
    - `wiz.yml`
    - `.env`, like:
      ---
      ENV_VAR_1=foo2
      ENV_VAR_2=baz
      ---

Run `deployk8s wiz setup {wiz_dir_env_path}` to setup. Follow prompts


## Deploy

Use the `deployk8s --help` for options

- Push local secrets, e.g., `deployk8s wiz push helm/dev`
- To see what helm values with be deployed, e.g., `deployk8s wiz genvalues helm/dev`
- To release e.g., `deployk8s wiz release helm/dev ghcr.io/topher515/foobar:latest-main`
