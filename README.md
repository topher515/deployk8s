# Help managing k8s + helm

## Setup this script

- Clone or copy this project
- Add `<this project>/bin` to your `$PATH`
- Run the `wizk8s` binary

## Setup your project to work with wizk8s

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

So now you have several "dir envs" like `<project_name>/helm/staging-3`.

Now `cd` to a dir env.

Run `wizk8s setup` to setup. Follow prompts.


## Deploy

Use the `wizk8s --help` for options

- Push local secrets, e.g., `wizk8s --dirpath=helm/dev push`
- To see what helm values with be deployed, e.g., `wizk8s --dirpath=helm/dev genvalues`
- To release e.g., `wizk8s --dirpath=helm/dev release ghcr.io/topher515/foobar:latest-main`
