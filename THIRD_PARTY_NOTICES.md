# 第三方组件与许可

本文件记录千枢源码分发与离线包中第三方组件的许可信息，是 [LICENSE](LICENSE)（Apache License 2.0）的补充。项目自身代码按 Apache-2.0 授权，见 [NOTICE](NOTICE)。项目名称、Logo（`frontend/public/logo.png`）和商标不按 Apache-2.0 授权，按项目方确认的授权使用。

> 生成口径：2026-11-18；后端取自已验证的 Python 3.9.25 环境（直接依赖版本与 `backend/constraints.txt` 一致），前端取自 `frontend/package-lock.json`。完整闭包随依赖升级变化，打包发布前必须重新生成并复核。

## 许可结论

- 未发现无许可声明或 `UNKNOWN` 许可的组件；未修改任何第三方源码，均以未修改的 wheel / npm 包形式使用。
- 非宽松许可的组件及处理方式：
  - `psycopg2-binary`：LGPL with exceptions（PSF 风格例外，允许随应用分发二进制）。本项目仅作为 PostgreSQL 驱动动态使用，不修改其源码。
  - `certifi`：MPL-2.0；未修改源码，保留其许可文本即可。
  - `python-dateutil`：BSD-3-Clause / Apache-2.0 双许可，按 Apache-2.0 分支使用。
  - `pycryptodome`：BSD-2-Clause（部分文件为公有领域）。
  - `regex`：Apache-2.0 AND CNRI-Python；`greenlet`：MIT AND Python-2.0；`pako`：MIT AND Zlib。同时满足各条款。
  - `dompurify`：MPL-2.0 OR Apache-2.0，按 Apache-2.0 分支使用。
  - `argparse`（npm，markdown-it 的传递依赖）：PSF-2.0。
  - `rgbcolor`：`MIT OR SEE LICENSE IN FEEL-FREE.md`，按 MIT 分支使用。
- 分发要求：保留各组件的版权与许可文本；Apache-2.0 组件若自带 NOTICE 须一并保留。离线包 `vendor/` 中的 wheel 自带 LICENSE/NOTICE，打包和再分发时不得删除。预构建前端产物中的第三方代码按上述 npm 许可证分发。

## 后端直接依赖

| 包 | 版本 | 许可证 |
|---|---|---|
| `alibabacloud-actiontrail20171204` | 1.0.2 | Apache License 2.0 |
| `alibabacloud-bssopenapi20171214` | 5.1.1 | Apache License 2.0 |
| `alibabacloud-cdn20180510` | 9.2.0 | Apache License 2.0 |
| `alibabacloud-cms20190101` | 4.0.10 | Apache License 2.0 |
| `alibabacloud-cs20151215` | 7.1.3 | Apache License 2.0 |
| `alibabacloud-das20200116` | 3.0.0 | Apache License 2.0 |
| `alibabacloud-ecs20140526` | 7.10.0 | Apache License 2.0 |
| `alibabacloud-elasticsearch20170613` | 5.3.4 | Apache License 2.0 |
| `alibabacloud-r-kvstore20150101` | 6.6.0 | Apache License 2.0 |
| `alibabacloud-rds20140815` | 15.9.1 | Apache License 2.0 |
| `alibabacloud-slb20140515` | 3.3.31 | Apache License 2.0 |
| `alibabacloud-tea-openapi` | 0.4.5 | Apache License 2.0 |
| `alibabacloud-waf-openapi20211001` | 7.8.5 | Apache License 2.0 |
| `aliyun-log-python-sdk` | 0.9.50 | MIT License |
| `aliyun-python-sdk-core` | 2.16.1 | Apache License 2.0 |
| `bcrypt` | 4.0.1 | Apache License, Version 2.0 |
| `cryptography` | 42.0.2 | Apache-2.0 OR BSD-3-Clause |
| `fastapi` | 0.110.0 | MIT |
| `httpx` | 0.28.1 | BSD-3-Clause |
| `oss2` | 2.19.1 | MIT License |
| `passlib` | 1.7.4 | BSD |
| `psycopg2-binary` | 2.9.9 | LGPL with exceptions |
| `pydantic` | 2.13.4 | MIT |
| `pydantic-settings` | 2.11.0 | MIT |
| `PyJWT` | 2.13.0 | MIT |
| `PyMySQL` | 1.2.0 | MIT |
| `python-multipart` | 0.0.9 | Apache-2.0 |
| `pyvmomi` | 8.0.3.0.1 | License :: OSI Approved :: Apache Software License |
| `redis` | 7.0.1 | MIT |
| `SQLAlchemy` | 2.0.27 | MIT |
| `tencentcloud-sdk-python` | 3.1.199 | Apache License 2.0 |
| `uvicorn` | 0.39.0 | BSD-3-Clause |
| `websocket-client` | 1.9.0 | Apache-2.0 |

## 后端完整依赖闭包（含传递依赖）

| 包 | 版本 | 许可证 |
|---|---|---|
| `aiofiles` | 23.2.1 | Apache-2.0 |
| `aiohappyeyeballs` | 2.6.1 | PSF-2.0 |
| `aiohttp` | 3.13.5 | Apache-2.0 AND MIT |
| `aiosignal` | 1.4.0 | Apache 2.0 |
| `alibabacloud-credentials` | 1.0.11 | Apache License 2.0 |
| `alibabacloud-credentials-api` | 1.0.1 | Apache License 2.0 |
| `alibabacloud-gateway-spi` | 0.0.4 | Apache License 2.0 |
| `alibabacloud-openapi-util` | 0.2.4 | Apache License 2.0 |
| `alibabacloud-tea` | 0.4.3 | Apache License 2.0 |
| `alibabacloud-tea-util` | 0.3.15 | Apache License 2.0 |
| `alibabacloud_endpoint_util` | 0.0.4 | Apache License 2.0 |
| `aliyun-python-sdk-kms` | 2.16.5 | Apache |
| `annotated-types` | 0.7.0 | MIT License |
| `anyio` | 4.12.1 | MIT |
| `APScheduler` | 3.11.3 | MIT |
| `async-timeout` | 5.0.1 | Apache 2 |
| `attrs` | 26.1.0 | MIT |
| `certifi` | 2026.7.22 | MPL-2.0 |
| `cffi` | 2.0.0 | MIT |
| `charset-normalizer` | 3.4.9 | MIT |
| `click` | 8.1.8 | BSD License |
| `cos_python_sdk_v5` | 1.9.44 | MIT |
| `crcmod` | 1.7 | MIT |
| `darabonba-core` | 1.0.8 | Apache License 2.0 |
| `dateparser` | 1.2.2 | BSD |
| `elastic-transport` | 9.1.0 | Apache Software License |
| `elasticsearch` | 9.1.3 | Apache-2.0 |
| `exceptiongroup` | 1.3.1 | MIT License |
| `frozenlist` | 1.8.0 | Apache-2.0 |
| `greenlet` | 3.2.5 | MIT AND Python-2.0 |
| `h11` | 0.16.0 | MIT |
| `httpcore` | 1.0.9 | BSD-3-Clause |
| `idna` | 3.14 | BSD-3-Clause |
| `jmespath` | 1.1.0 | MIT |
| `lz4` | 4.4.5 | BSD License |
| `multidict` | 6.7.1 | Apache License 2.0 |
| `propcache` | 0.4.1 | Apache-2.0 |
| `protobuf` | 5.29.6 | 3-Clause BSD License |
| `pycparser` | 2.23 | BSD-3-Clause |
| `pycryptodome` | 3.23.0 | BSD, Public Domain |
| `pydantic_core` | 2.46.4 | MIT |
| `python-dateutil` | 2.9.0.post0 | Dual License |
| `python-dotenv` | 1.2.1 | BSD-3-Clause |
| `pytz` | 2026.3.post1 | MIT |
| `regex` | 2026.1.15 | Apache-2.0 AND CNRI-Python |
| `requests` | 2.32.5 | Apache-2.0 |
| `six` | 1.15.0 | MIT |
| `starlette` | 0.36.3 | BSD-3-Clause |
| `typing-inspection` | 0.4.2 | MIT |
| `typing_extensions` | 4.15.0 | PSF-2.0 |
| `tzlocal` | 5.3.1 | MIT |
| `urllib3` | 2.6.3 | MIT |
| `xmltodict` | 1.0.4 | MIT |
| `yarl` | 1.22.0 | Apache-2.0 |

## 前端直接依赖

| 包 | 版本 | 许可证 |
|---|---|---|
| `@vitejs/plugin-vue` | 5.2.4 | MIT |
| `@xterm/addon-fit` | 0.11.0 | MIT |
| `@xterm/xterm` | 6.0.0 | MIT |
| `axios` | 1.19.0 | MIT |
| `echarts` | 5.6.0 | Apache-2.0 |
| `element-plus` | 2.14.5 | MIT |
| `html2pdf.js` | 0.14.0 | MIT |
| `markdown-it` | 15.0.0 | MIT |
| `pinia` | 2.3.1 | MIT |
| `vite` | 5.4.21 | MIT |
| `vue` | 3.5.41 | MIT |
| `vue-echarts` | 7.0.3 | MIT |
| `vue-router` | 4.6.4 | MIT |

## 前端完整依赖闭包（含传递依赖）

| 包 | 版本 | 许可证 |
|---|---|---|
| `@babel/helper-string-parser` | 7.29.7 | MIT |
| `@babel/helper-validator-identifier` | 7.29.7 | MIT |
| `@babel/parser` | 7.29.8 | MIT |
| `@babel/runtime` | 7.29.7 | MIT |
| `@babel/types` | 7.29.8 | MIT |
| `@ctrl/tinycolor` | 4.2.0 | MIT |
| `@element-plus/icons-vue` | 2.3.2 | MIT |
| `@esbuild/aix-ppc64` | 0.21.5 | MIT |
| `@esbuild/android-arm` | 0.21.5 | MIT |
| `@esbuild/android-arm64` | 0.21.5 | MIT |
| `@esbuild/android-x64` | 0.21.5 | MIT |
| `@esbuild/darwin-arm64` | 0.21.5 | MIT |
| `@esbuild/darwin-x64` | 0.21.5 | MIT |
| `@esbuild/freebsd-arm64` | 0.21.5 | MIT |
| `@esbuild/freebsd-x64` | 0.21.5 | MIT |
| `@esbuild/linux-arm` | 0.21.5 | MIT |
| `@esbuild/linux-arm64` | 0.21.5 | MIT |
| `@esbuild/linux-ia32` | 0.21.5 | MIT |
| `@esbuild/linux-loong64` | 0.21.5 | MIT |
| `@esbuild/linux-mips64el` | 0.21.5 | MIT |
| `@esbuild/linux-ppc64` | 0.21.5 | MIT |
| `@esbuild/linux-riscv64` | 0.21.5 | MIT |
| `@esbuild/linux-s390x` | 0.21.5 | MIT |
| `@esbuild/linux-x64` | 0.21.5 | MIT |
| `@esbuild/netbsd-x64` | 0.21.5 | MIT |
| `@esbuild/openbsd-x64` | 0.21.5 | MIT |
| `@esbuild/sunos-x64` | 0.21.5 | MIT |
| `@esbuild/win32-arm64` | 0.21.5 | MIT |
| `@esbuild/win32-ia32` | 0.21.5 | MIT |
| `@esbuild/win32-x64` | 0.21.5 | MIT |
| `@floating-ui/core` | 1.8.0 | MIT |
| `@floating-ui/dom` | 1.8.0 | MIT |
| `@floating-ui/utils` | 0.2.12 | MIT |
| `@jridgewell/sourcemap-codec` | 1.5.5 | MIT |
| `@napi-rs/lzma-linux-x64-gnu` | 1.5.1 | MIT |
| `@popperjs/core` | 2.11.8 | MIT |
| `@rollup/rollup-android-arm-eabi` | 4.62.5 | MIT |
| `@rollup/rollup-android-arm64` | 4.62.5 | MIT |
| `@rollup/rollup-darwin-arm64` | 4.62.5 | MIT |
| `@rollup/rollup-darwin-x64` | 4.62.5 | MIT |
| `@rollup/rollup-freebsd-arm64` | 4.62.5 | MIT |
| `@rollup/rollup-freebsd-x64` | 4.62.5 | MIT |
| `@rollup/rollup-linux-arm-gnueabihf` | 4.62.5 | MIT |
| `@rollup/rollup-linux-arm-musleabihf` | 4.62.5 | MIT |
| `@rollup/rollup-linux-arm64-gnu` | 4.62.5 | MIT |
| `@rollup/rollup-linux-arm64-musl` | 4.62.5 | MIT |
| `@rollup/rollup-linux-loong64-gnu` | 4.62.5 | MIT |
| `@rollup/rollup-linux-loong64-musl` | 4.62.5 | MIT |
| `@rollup/rollup-linux-ppc64-gnu` | 4.62.5 | MIT |
| `@rollup/rollup-linux-ppc64-musl` | 4.62.5 | MIT |
| `@rollup/rollup-linux-riscv64-gnu` | 4.62.5 | MIT |
| `@rollup/rollup-linux-riscv64-musl` | 4.62.5 | MIT |
| `@rollup/rollup-linux-s390x-gnu` | 4.62.5 | MIT |
| `@rollup/rollup-linux-x64-gnu` | 4.62.5 | MIT |
| `@rollup/rollup-linux-x64-musl` | 4.62.5 | MIT |
| `@rollup/rollup-openbsd-x64` | 4.62.5 | MIT |
| `@rollup/rollup-openharmony-arm64` | 4.62.5 | MIT |
| `@rollup/rollup-win32-arm64-msvc` | 4.62.5 | MIT |
| `@rollup/rollup-win32-ia32-msvc` | 4.62.5 | MIT |
| `@rollup/rollup-win32-x64-gnu` | 4.62.5 | MIT |
| `@rollup/rollup-win32-x64-msvc` | 4.62.5 | MIT |
| `@types/estree` | 1.0.9 | MIT |
| `@types/lodash` | 4.17.25 | MIT |
| `@types/lodash-es` | 4.17.12 | MIT |
| `@types/pako` | 2.0.4 | MIT |
| `@types/raf` | 3.4.3 | MIT |
| `@types/trusted-types` | 2.0.7 | MIT |
| `@types/web-bluetooth` | 0.0.21 | MIT |
| `@vue/compiler-core` | 3.5.41 | MIT |
| `@vue/compiler-dom` | 3.5.41 | MIT |
| `@vue/compiler-sfc` | 3.5.41 | MIT |
| `@vue/compiler-ssr` | 3.5.41 | MIT |
| `@vue/devtools-api` | 6.6.4 | MIT |
| `@vue/reactivity` | 3.5.41 | MIT |
| `@vue/runtime-core` | 3.5.41 | MIT |
| `@vue/runtime-dom` | 3.5.41 | MIT |
| `@vue/server-renderer` | 3.5.41 | MIT |
| `@vue/shared` | 3.5.41 | MIT |
| `@vueuse/core` | 14.4.0 | MIT |
| `@vueuse/metadata` | 14.4.0 | MIT |
| `@vueuse/shared` | 14.4.0 | MIT |
| `agent-base` | 6.0.2 | MIT |
| `argparse` | 3.0.1 | PSF-2.0 |
| `async-validator` | 4.2.5 | MIT |
| `asynckit` | 0.4.0 | MIT |
| `base64-arraybuffer` | 1.0.2 | MIT |
| `call-bind-apply-helpers` | 1.0.2 | MIT |
| `canvg` | 3.0.11 | MIT |
| `combined-stream` | 1.0.8 | MIT |
| `core-js` | 3.50.0 | MIT |
| `css-line-break` | 2.1.0 | MIT |
| `csstype` | 3.2.3 | MIT |
| `dayjs` | 1.11.23 | MIT |
| `debug` | 4.4.3 | MIT |
| `delayed-stream` | 1.0.0 | MIT |
| `dompurify` | 3.4.14 | (MPL-2.0 OR Apache-2.0) |
| `dunder-proto` | 1.0.1 | MIT |
| `entities` | 7.0.1 | BSD-2-Clause |
| `es-define-property` | 1.0.1 | MIT |
| `es-errors` | 1.3.0 | MIT |
| `es-object-atoms` | 1.1.2 | MIT |
| `es-set-tostringtag` | 2.1.0 | MIT |
| `esbuild` | 0.21.5 | MIT |
| `estree-walker` | 2.0.2 | MIT |
| `fast-png` | 6.4.0 | MIT |
| `fflate` | 0.8.3 | MIT |
| `follow-redirects` | 1.16.0 | MIT |
| `form-data` | 4.0.6 | MIT |
| `fsevents` | 2.3.3 | MIT |
| `function-bind` | 1.1.2 | MIT |
| `get-intrinsic` | 1.3.0 | MIT |
| `get-proto` | 1.0.1 | MIT |
| `gopd` | 1.2.0 | MIT |
| `has-symbols` | 1.1.0 | MIT |
| `has-tostringtag` | 1.0.2 | MIT |
| `hasown` | 2.0.4 | MIT |
| `html2canvas` | 1.4.1 | MIT |
| `https-proxy-agent` | 5.0.1 | MIT |
| `iobuffer` | 5.4.0 | MIT |
| `jspdf` | 4.2.1 | MIT |
| `linkify-it` | 6.1.0 | MIT |
| `lodash` | 4.18.1 | MIT |
| `lodash-es` | 4.18.1 | MIT |
| `lodash-unified` | 1.0.3 | MIT |
| `magic-string` | 0.30.21 | MIT |
| `markdown-it/node_modules/entities` | 8.0.0 | BSD-2-Clause |
| `math-intrinsics` | 1.1.0 | MIT |
| `mdurl` | 2.1.0 | MIT |
| `memoize-one` | 6.0.0 | MIT |
| `mime-db` | 1.52.0 | MIT |
| `mime-types` | 2.1.35 | MIT |
| `ms` | 2.1.3 | MIT |
| `nanoid` | 3.3.18 | MIT |
| `normalize-wheel-es` | 1.2.0 | BSD-3-Clause |
| `pako` | 2.2.0 | (MIT AND Zlib) |
| `performance-now` | 2.1.0 | MIT |
| `picocolors` | 1.1.1 | ISC |
| `postcss` | 8.5.26 | MIT |
| `proxy-from-env` | 2.1.0 | MIT |
| `punycode.js` | 2.3.1 | MIT |
| `raf` | 3.4.1 | MIT |
| `regenerator-runtime` | 0.13.11 | MIT |
| `rgbcolor` | 1.0.1 | MIT OR SEE LICENSE IN FEEL-FREE.md |
| `rollup` | 4.62.5 | MIT |
| `source-map-js` | 1.2.1 | BSD-3-Clause |
| `stackblur-canvas` | 2.7.0 | MIT |
| `svg-pathdata` | 6.0.3 | MIT |
| `text-segmentation` | 1.0.3 | MIT |
| `tslib` | 2.3.0 | 0BSD |
| `uc.micro` | 3.0.0 | MIT |
| `utrie` | 1.0.2 | MIT |
| `vue-component-type-helpers` | 3.3.11 | MIT |
| `vue-demi` | 0.14.10 | MIT |
| `vue-echarts/node_modules/vue-demi` | 0.13.11 | MIT |
| `zrender` | 5.6.1 | BSD-3-Clause |

## 复核方法

依赖升级后按下面命令重新导出并比对。任何组件出现 `UNKNOWN`、许可缺失或与本节记录不一致，都必须先解决再打包。

```bash
# 后端：在安装好 backend/requirements.txt 的 venv 中执行
cd backend
python3 - <<'EOF'
import importlib.metadata as md
for d in sorted(md.distributions(), key=lambda x: (x.metadata['Name'] or '').lower()):
    n = d.metadata['Name']
    if not n:
        continue
    lic = d.metadata.get('License-Expression') or d.metadata.get('License') or 'UNKNOWN'
    print(f"{n}=={d.version}\t{' '.join(str(lic).split())[:100]}")
EOF

# 前端：从 lock 文件读取每个包的声明许可
cd frontend
node - <<'EOF'
const lock = require('./package-lock.json')
for (const [k, v] of Object.entries(lock.packages)) {
  if (!k.startsWith('node_modules/') || v.link) continue
  console.log(`${k.slice(13)} ${v.version} ${v.license || 'UNKNOWN'}`)
}
EOF
```

许可证全文随各依赖包提供；也可从上游仓库或 PyPI / npm 对应版本获取。

