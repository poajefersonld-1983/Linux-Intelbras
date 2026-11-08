# Histórico de alterações

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
