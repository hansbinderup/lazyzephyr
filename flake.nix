{
  description = "lazyzephyr - lazygit-style terminal UI for Zephyr: build, devicetree, Kconfig and twister tests";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      packages = forAll (pkgs: rec {
        lazyzephyr = pkgs.python3Packages.buildPythonApplication {
          pname = "lazyzephyr";
          version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = with pkgs.python3Packages; [ textual watchfiles pyyaml pyte ];
          nativeCheckInputs = with pkgs.python3Packages; [ pytestCheckHook pytest-asyncio ];
          # Relies on live filesystem watching, which is unreliable in the Nix build sandbox.
          disabledTests = [ "test_new_testcase_is_picked_up_live" ];
          meta = {
            description = "lazygit-style terminal UI for Zephyr: build, devicetree, Kconfig and twister tests";
            homepage = "https://github.com/hansbinderup/lazyzephyr";
            license = pkgs.lib.licenses.asl20;
            mainProgram = "lazyzephyr";
          };
        };
        default = lazyzephyr;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [
              textual watchfiles pyyaml pyte pytest pytest-asyncio
            ]))
          ];
        };
      });
    };
}
