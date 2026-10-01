# nixkube / nix + kube / nix-csi / nix-nri

[![Documentation](https://img.shields.io/badge/docs-lillecarl.github.io-blue)](https://lillecarl.github.io/nixkube/)

Mount /nix into Kubernetes pods using the CSI ephemeral volumes or NRI(Node Resource Interface). Volumes
share lifetime with Pods and are embedded into the Podspec.

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Lillecarl/nix-csi)

## Deploying nixkube

Stick your pubkeys in ./keys and they will be imported into the module system
then run the following command and you'll have nixkube deployed.
```bash
nix run --file . kubenixEval.deploymentScript -- --yes --prune
```

If you'd rather mangle YAML yourself you can use
```bash
nix build --file . easykubenix.manifestYAMLFile
```
and stuff the result into Kustomize, a blender or your Kubernetes cluster

## Deploying workloads

nixkube supports two methods for injecting Nix stores into pods:

### CSI Ephemeral Volumes (Explicit)

Request Nix stores explicitly via CSI volumeAttributes. Specify one or more:
- `storePath` - Direct nix store path (highest priority)
- `flakeRef` - Flake reference to build
- `nixExpr` - Nix expression to evaluate and build

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: hello-csi
spec:
  containers:
  - name: hello
    image: nixos/nix:latest
    volumeMounts:
    - name: nix
      mountPath: /nix
  volumes:
  - name: nix
    ephemeral:
      volumeClaimTemplate:
        spec:
          accessModes: ["ReadOnlyMany"]
          storageClassName: ephemeral-storage
          resources:
            requests:
              storage: 1Gi
          csi:
            driver: nixkube
            volumeAttributes:
              x86_64-linux: /nix/store/hello-......
              aarch64-linux: /nix/store/hello-......
              flakeRef: github:nixos/nixpkgs/nixos-unstable#hello
              nixExpr: |
                let
                  nixpkgs = builtins.fetchTree {
                    type = "github";
                    owner = "nixos";
                    repo = "nixpkgs";
                    ref = "nixos-unstable";
                  };
                  pkgs = import nixpkgs { };
                in
                pkgs.hello
```

The first successful option by priority wins.

**Tip**: For command arrays and environment variables, use `lib.getExe` to reference executables without managing full store paths:

```nix
command = [ (lib.getExe pkgs.hello) ]
env = {
  name = "HELLO_CONFIG";
  value = pkgs.hello-config;
}
```

### NRI Plugin (Automatic via Annotations)

Use pod annotations to automatically inject Nix stores without explicit volume requests:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: hello-nri
  annotations:
    nix-nri/pod: |
      /nix/store/hello-......
    nix-nri/pod@aarch64-linux: |
      /nix/store/hello-aarch64-......
spec:
  containers:
  - name: hello
    image: nixos/nix:latest
    # /nix is automatically mounted by NRI plugin
```

Examples:
* [multi-system example](https://github.com/Lillecarl/hetzkube/blob/4ed76ec77bfb104d1c2307b1ba178efa61dd34e2/kubenix/modules/cheapam.nix#L113)
* [single-system ci example(s)](https://github.com/Lillecarl/nix-csi/blob/3179e5f8383e760bbef313300a224e44f18722c7/kubenix/ci/default.nix)

#### Flake refs as mount sources

A mount's source may be a flake ref (or a Nix expression) instead of a store
path; the node realises it before the container starts:

```yaml
metadata:
  annotations:
    nixkube/app: /app=github:NixOS/nixpkgs/<rev>#redis
    nixkube/pod-pull: if-not-present   # optional: always (default) | if-not-present
    nixkube/pod-build: substitute      # optional: tighten the node's build policy
```

Resolution follows OCI's pull semantics. A store path is fetched if
missing. A **locked** ref (one naming a revision or a `narHash`) is resolved
once per node and remembered, as a digest would be. A **floating** ref
(a branch, or a registry name like `nixpkgs#hello`) follows the pull policy:
`always` re-resolves it at every container start, refetching rather than
trusting Nix's fetch cache; `if-not-present` reuses what the node resolved
before. Nodes do not coordinate, so pin refs that must match everywhere.

With `nixkube.node.recordResolved = true`, nixkube annotates each pod with
what its flake-ref sources resolved to (`resolved.nixkube/<container>`), as
a container status names its image digest. It's off by default because it
needs `patch` on pods; nothing else NRI does writes to the API.

What a node may do to realise a source is its build policy
(`nixkube.node.buildPolicy`): `build`, `substitute` (evaluate, only fetch) or
`pinned` (store paths only). A pod may tighten it, never loosen it.

[Documentation](https://lillecarl.github.io/nixkube/)

---

*This project is made possible by*

[![Dynamist](.assets/dynamist-logo.png)](https://dynamist.se/)

