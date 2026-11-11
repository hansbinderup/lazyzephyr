{
  description = "lazydts - lazygit-style TUI for Zephyr devicetrees";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      packages = forAll (pkgs: rec {
        lazydts = pkgs.python3Packages.buildPythonApplication {
          pname = "lazydts";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = with pkgs.python3Packages; [ textual pyyaml ];
          nativeCheckInputs = with pkgs.python3Packages; [ pytestCheckHook pytest-asyncio ];
          meta = {
            description = "lazygit-style TUI for Zephyr devicetrees in west workspaces";
            mainProgram = "lazydts";
          };
        };
        default = lazydts;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [
              textual pyyaml pytest pytest-asyncio
            ]))
          ];
        };
      });
    };
}
