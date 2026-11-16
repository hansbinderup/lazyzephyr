# Changelog

## [0.2.0](https://github.com/hansbinderup/lazyzephyr/compare/v0.1.0...v0.2.0) (2026-11-16)


### Features

* **build:** Select and copy text in the monitor ([6061249](https://github.com/hansbinderup/lazyzephyr/commit/606124936a9afca3d2bcf30a37bfddb015bd138f))
* **build:** Show the copy key in the monitor footer ([1e3f15c](https://github.com/hansbinderup/lazyzephyr/commit/1e3f15c4d3f6065312d65a1b78164982e017fcfd))
* **tests:** Filter to the chosen platforms ([975de5f](https://github.com/hansbinderup/lazyzephyr/commit/975de5f3ceb4c1724a6545d1ce7f97e1c44f5185))
* **tests:** Fit the status line and footer to the width ([85a9793](https://github.com/hansbinderup/lazyzephyr/commit/85a9793785341e659cceee355cf759aae83a30f4))
* **tests:** Remember the settings as last used ([265b0d1](https://github.com/hansbinderup/lazyzephyr/commit/265b0d1bf389bd2a1c8407762c039ed28cb07b4b))
* **tests:** Run on a board and show case results ([9cb564a](https://github.com/hansbinderup/lazyzephyr/commit/9cb564adbb3f5b1ec958c7aa418ffb36acf0de2b))
* **tests:** Set up a board per platform ([2f7afeb](https://github.com/hansbinderup/lazyzephyr/commit/2f7afeb392c407e03408428b3d5738ee9e816494))
* **tests:** Show each test's output ([52af171](https://github.com/hansbinderup/lazyzephyr/commit/52af171516ff3840186ee678769fac2a491f9358))


### Fixes

* **build:** Keep selected monitor text readable ([4fb0fdb](https://github.com/hansbinderup/lazyzephyr/commit/4fb0fdb5c9ebcb6e70027ba9d53f2e0d31da3da0))
* Copy to the system clipboard, not only via the terminal ([1fee2ea](https://github.com/hansbinderup/lazyzephyr/commit/1fee2ea5b62687e1aeb2700b85760c6d9c1626d9))
* **tests:** List a cancelled job once until twister exits ([d12fa05](https://github.com/hansbinderup/lazyzephyr/commit/d12fa0552bbfbcc0c4170d4ae00a44d4c0ba5fb9))


### Documentation

* Add guidelines for AI assistants ([6866064](https://github.com/hansbinderup/lazyzephyr/commit/6866064cf684781d49d415ba809462db64789711))

## 0.1.0 (2026-11-12)


### Features

* **build:** Add a Build tab ([cde969a](https://github.com/hansbinderup/lazyzephyr/commit/cde969af3af011cd821633aba84c0572d2bda29e))
* **build:** Add a serial monitor ([b178f63](https://github.com/hansbinderup/lazyzephyr/commit/b178f63aeb381d967a2706b074b975786347fdbb))
* **build:** Edit build configs as a list of settings ([6afafa1](https://github.com/hansbinderup/lazyzephyr/commit/6afafa18faa16385daade41ccb8d7387bfc61be2))
* **build:** Put the build details under the configs list ([4b40af3](https://github.com/hansbinderup/lazyzephyr/commit/4b40af3df3da4d1eb647128881f81709f2fa9745))
* Combine lazydts and lazytwister into lazyzephyr ([0a94e53](https://github.com/hansbinderup/lazyzephyr/commit/0a94e53afccd99ca40304ae717facfe42b4ef89b))
* **dts:** Add a property reference card on i ([02f9831](https://github.com/hansbinderup/lazyzephyr/commit/02f9831ab4f1f263b5ac57cac05512234c1a8821))
* **dts:** Add lazydts, a TUI for Zephyr devicetrees ([019eccf](https://github.com/hansbinderup/lazyzephyr/commit/019eccffe83a0e4379459fc9594167509674d2e3))
* **dts:** Edit a node as a short list of settings ([87a1cf1](https://github.com/hansbinderup/lazyzephyr/commit/87a1cf11c7dd201a9c6839dcd0772384cf89b754))
* **dts:** Edit phandle-arrays as controller and cells ([e927e2b](https://github.com/hansbinderup/lazyzephyr/commit/e927e2b822226b1e9d446a58339a966c4573c34f))
* **dts:** Pick labels from a list; edit /chosen and /aliases ([983f4eb](https://github.com/hansbinderup/lazyzephyr/commit/983f4eb0f485e40c42fdfd525b447df297194c23))
* **dts:** Show references to a node and list problems ([fb98cd1](https://github.com/hansbinderup/lazyzephyr/commit/fb98cd1274a83738f1a0ac5b6e7ec319608d0f59))
* **kconfig:** Add a Kconfig tab ([f7d648d](https://github.com/hansbinderup/lazyzephyr/commit/f7d648d85de98fd1d0f26a64000a0a88ccd62cd7))
* Order the tabs Build, Devicetree, Tests ([a0a521c](https://github.com/hansbinderup/lazyzephyr/commit/a0a521c626704bb5985e3de2b337e3137f713537))
* Resize and move panels in every tab ([f3cec89](https://github.com/hansbinderup/lazyzephyr/commit/f3cec89fb81555d80d8f9450e7e163015474356c))


### Fixes

* **build:** Add monitor keys that work inside the monitor ([9eaa22f](https://github.com/hansbinderup/lazyzephyr/commit/9eaa22f80b0b91d7d18a72b4ca5f2bdf62e7140a))
* **dts:** Fix crash when opening the property editor ([e7e4d76](https://github.com/hansbinderup/lazyzephyr/commit/e7e4d765f3cc93f1952e3ef877cb8c9c73fea11b))
* Fix full screen inside the tabbed app ([87cfedb](https://github.com/hansbinderup/lazyzephyr/commit/87cfedb53476932f943e0a59cf1835bfdc178687))
* Fix two crashes in adding nodes and Kconfig navigation ([ce7bf1c](https://github.com/hansbinderup/lazyzephyr/commit/ce7bf1c2cc17ed477b13ec02914182bc6fedac01))
* Stop tabs jumping on start; Kconfig next to Devicetree ([dfb53cd](https://github.com/hansbinderup/lazyzephyr/commit/dfb53cd6f9b7c97fb619e4fdaaaf632202e2cab0))


### Documentation

* Move controls to docs/; lead the README with install ([50f6fe5](https://github.com/hansbinderup/lazyzephyr/commit/50f6fe511d0bf126093c632fe591d39e354fb821))
