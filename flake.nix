{
  description = "lazyzephyr - lazygit-style TUI for Zephyr: twister tests and the devicetree";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      packages = forAll (pkgs: rec {
        lazyzephyr = pkgs.python3Packages.buildPythonApplication {
          pname = "lazyzephyr";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = with pkgs.python3Packages; [ textual watchfiles pyyaml ];
          nativeCheckInputs = with pkgs.python3Packages; [ pytestCheckHook pytest-asyncio ];
          # Relies on live filesystem watching, which is unreliable in the Nix build sandbox.
          disabledTests = [ "test_new_testcase_is_picked_up_live" ];
          meta = {
            description = "lazygit-style TUI for Zephyr: twister tests and the devicetree";
            mainProgram = "lazyzephyr";
          };
        };
        default = lazyzephyr;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [
              textual watchfiles pyyaml pytest pytest-asyncio
            ]))
          ];
        };
      });
    };
}
