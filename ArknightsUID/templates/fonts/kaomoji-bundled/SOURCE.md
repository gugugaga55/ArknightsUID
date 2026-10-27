# Bundled kaomoji / symbol fallback fonts

Official repository: https://github.com/notofonts/noto-fonts (formerly googlefonts/noto-fonts).
Pinned commit: `ffebf8c1ee449e544955a7e813c54f9b73848eac`.
The five original TTF files come from `hinted/ttf/<family-without-spaces>/<filename>` at this exact commit; the explicitly listed Bamum supplement comes from its unhinted OTF directory, verified against the official tree blob SHA-1.

License: SIL Open Font License 1.1. `OFL.txt` is an unmodified copy of that commit's root `LICENSE`, blob `c82d72e422e2d08c5ab439b6bac7c2177ea0c565`. Root copyright: Copyright 2018 The Noto Project Authors (github.com/googlei18n/noto-fonts). Individual embedded copyright and license notices remain in each original font. Preserve these notices and the OFL when redistributing. No fonts were renamed or modified. OFL permits bundling under its conditions; fonts must not be sold by themselves.

| File | Bytes | CSS family | SHA-256 |
|---|---:|---|---|
| NotoSans-Regular.ttf | 569,208 | Noto Sans | `b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5` |
| NotoSansCanadianAboriginal-Regular.ttf | 113,740 | Noto Sans CanAborig | `70735441641a0a90f9f1baa45c7340deea77f19c5829fc71162bb443acad2ca7` |
| NotoSansMalayalam-Regular.ttf | 111,724 | Noto Sans Malayalam | `42eb462ff13e820ebbeaaec4e7b426bd1de145d02cef8b634f5a3efd376b513c` |
| NotoSansSymbols2-Regular.ttf | 656,852 | Noto Sans Symbols2 | `630846d528dbe4c4981370a4d0a9475a1fd1491a129bb411f8e157cdb5de13c6` |
| NotoSansYi-Regular.ttf | 182,808 | Noto Sans Yi | `6cf1952d73528dd20f1238184054e0f2c36e039941d46400dc9ca865b73a1cb4` |

Total: 1,634,332 font bytes. The original five TTFs are exactly the five pre-existing default font selections in ArknightsUID's kaomoji_fonts module; downloading all other scripts is not required. Family names and targeted character coverage were verified with fc-scan and the OpenType name table. The sample supplementary-plane character U+16966 𖥦 is Bamum, not Lisu, and is absent from all five fonts.

## Explicit documented-sample supplement

`NotoSansBamum-Regular.otf` from the same pin, path `unhinted/otf/NotoSansBamum/NotoSansBamum-Regular.otf`, blob `5727754429119ca37b562998e555df1dbc7bc602`, 105,252 bytes, SHA-256 `26af087f080ab2c503fa308bc620939337df1e79e45d1ea6c7cb6d31c324814e`. Exact family: `Noto Sans Bamum`. Same root OFL applies. Added only to cover existing documented sample `ദ്ദി˶>𖥦<)`: Python unicodedata identifies 𖥦 as U+16966 BAMUM LETTER PHASE-E NDAP; fc-scan confirms cmap range 16800–16A38 covers this codepoint. The other five fonts lack it. Six-font total: 1,739,584 bytes.

Exact family note: fc-scan reports `Noto Sans Canadian Aboriginal,Noto Sans CanAborig` for Canadian Aboriginal. Symbols2 has family `Noto Sans Symbols2` (no space before 2); use this exact embedded name for renderers that match registered fonts by family. A browser @font-face declaration can define a CSS alias independently.
