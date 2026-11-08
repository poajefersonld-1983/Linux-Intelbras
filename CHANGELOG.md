# Histórico de alterações

## 0.15.0 — 08/11/2026

- Exibe câmeras em cartões maiores, organizados em duas colunas.
- Adiciona busca por número exato ou nome do cliente e indica estados de recepção.
- Mostra a tela de destino e o botão Abrir seleção no painel principal.
- Seleciona a tela de câmeras já abertas sem reiniciar vídeo ou áudio.
- Exclui câmeras ocultas pela busca da abertura e do arraste em grupo.
- Preserva o uso do painel em janelas de 1280×800.

## 0.14.0 — 08/11/2026

- Abre os canais 1 a 4 ao clicar ou ativar um cliente.
- Preserva transmissões e áudio já abertos ao repetir a seleção.
- Confere a autenticação antes de substituir o mosaico existente.
- Amplia os cartões de clientes e adiciona busca por nome e largura ajustável.
- Agrupa controles avançados em Mais opções, sem reduzir a lista principal.
- Mantém a abertura do aplicativo sem conexões automáticas.

## 0.13.1 — 08/11/2026

- Troca o checkbox de áudio por um botão explícito Ativar áudio / Desativar áudio.
- Durante a conexão, o botão Cancelar áudio permite interromper a tentativa.
- Falhas de áudio e desconexão restauram o botão ao estado desligado.

## 0.13.0 — 08/11/2026

- Habilita áudio nos canais Intelbras Cloud pelo checkbox Áudio.
- Solicita áudio separadamente, sem duplicar a transmissão de vídeo.
- Usa saída Qt nativa, filas limitadas e encerramento do áudio sem desconectar a imagem.
- Trata canais sem áudio e falhas de saída sem interromper o vídeo.
- Corrige a proporção dos canais 1080N (960×1080), mantendo as outras proporções.
- Usa a taxa de entrada da câmera para a apresentação; ignora a taxa estimada do rawvideo.
- Mantém as conexões HD separadas e a cadência regular dos ajustes anteriores.
- Habilita escala VAAPI no iniciador, com fallback para CPU.

Os testes automatizados usam fontes sintéticas. Credenciais, cadastros, capturas e gravações reais não integram o projeto publicado.
