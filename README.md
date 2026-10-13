# Linux-Intelbras

Monitor de câmeras para Linux desenvolvido por **Jeferson Lackman**.

Aplicativo independente, com interface PyQt6, para abrir câmeras por RTSP ou Intelbras Cloud/P2P com configuração local autorizada.

## Recursos

- Login da Conta Intelbras e importação do catálogo iSIC Lite.
- Transporte IPv4 direto e por relay, com parâmetros fornecidos localmente.
- Mosaicos de 1, 4 ou 9 quadros, combinando câmeras de clientes diferentes.
- Clique ou arraste para escolher câmera e quadro.
- HD por padrão; escolha SD/HD por quadro.
- Tela cheia apenas com imagens; F11 e Esc para entrar/sair.
- Captura de imagem e indicação de vídeo recebido.
- Senhas e tokens somente na memória da sessão.

## Instalação no Debian 13

Baixe o pacote `.deb` na página de [releases](https://github.com/poajefersonld-1983/Linux-Intelbras/releases) e instale:

```sh
sudo apt install ./linux-intelbras_0.12.0-1_all.deb
linux-intelbras
```

Dependências: Python 3.11+, PyQt6 6.8+ com multimídia e FFmpeg 7.1+. O pacote atualiza a instalação anterior do Monitor Linux e mantém os dados locais.

## Compilar e testar

```sh
python3 scripts/build_deb.py
PYTHONPATH=src QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -q
```

## Configuração privada

**O repositório e o pacote não incluem credenciais, tokens, parâmetros privados de aplicação, equipamentos reais ou imagens de câmeras.** RTSP pode ser configurado diretamente na interface. O uso da Conta Intelbras/Cloud depende de configuração própria e autorizada.

Para compatibilidade com versões anteriores, o aplicativo mantém os caminhos locais abaixo (ou os correspondentes diretórios XDG):

- `~/.config/simnext-linux/cloud.json`: identificador OAuth autorizado, conforme `examples/cloud.example.json`.
- `~/.config/simnext-linux/p2p.json`: parâmetros autorizados do transporte, conforme `examples/p2p.example.json`.
- `~/.local/share/simnext-linux/`: cadastro SQLite e preferências de canais, somente neste computador.

Copie os exemplos para arquivos **locais**, preencha seus próprios parâmetros e proteja-os com permissões `600`. Não envie esses arquivos, bancos de dados ou capturas ao GitHub. Não há provisionamento público dos parâmetros privados nesta distribuição. Após fechar o aplicativo, autentique novamente para obter as credenciais dos equipamentos.

## Limitações

Compatibilidade varia conforme o gravador e sua configuração. O transporte implementa IPv4/VDT; IPv6 e equipamentos com outros protocolos não foram validados. Áudio Cloud não está habilitado. SD usa o fluxo extra e HD usa o principal, conforme configurados no equipamento. O vídeo Cloud é renderizado em 640×360 ou 1280×720, até 10 quadros/s. Textos incorporados pelo próprio gravador permanecem no vídeo.

## Desenvolvimento e licença

**Desenvolvedor: Jeferson Lackman.** Código do projeto sob GPL-3.0-or-later; consulte [COPYRIGHT](COPYRIGHT) e [LICENSE](LICENSE).

O transporte base deriva de [intelbras-isic-ha](https://github.com/eduardobittencourt/intelbras-isic-ha), de Eduardo Bittencourt, revisão `8fa2d62bb1f850b93f303af10701e1a0b975e6e1`, sob MIT. Sua licença e autoria são preservadas em [packaging/isic-transport-LICENSE](packaging/isic-transport-LICENSE). Este projeto acrescenta integração de interface, gerenciamento de sessões e suporte a relay.
