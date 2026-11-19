# Base de ajuda do Commercia (painel web)

> RASCUNHO escrito a partir das regras da API. O time do painel deve revisar os nomes das
> telas e botões (marcados com [tela?]) antes de considerar esta base final.

## O que é o Commercia
Sistema de gestão para pequenos varejistas: cadastro de produtos, clientes e vendas, estoque,
relatórios, análises de vendas e previsão de vendas. O painel web é usado pela administração;
o app Android é usado no balcão e funciona mesmo sem internet.

## Lojas
- Uma pessoa pode ter acesso a uma ou mais lojas. Quem tem várias lojas vê relatórios de cada
  uma ou de todas somadas, e pode compará-las.
- Cada loja tem o próprio catálogo: o mesmo produto vendido em duas lojas é cadastrado em
  cada uma, com preço e estoque próprios.

## Perfis de acesso
- **Administrador**: tudo — usuários, catálogo, clientes, fornecedores, vendas e o log de auditoria.
- **Gerente**: catálogo (produtos, preços, estoque, categorias), clientes, fornecedores e vendas.
  Não gerencia usuários nem vê o log de auditoria.
- **Vendedor**: registra vendas e cadastra clientes. Não altera preço nem estoque.
- Todos os perfis veem os relatórios das lojas a que têm acesso.

## Usuários [tela?: Usuários]
- Cadastro pede nome completo, usuário (login, sem espaços), e-mail, senha (mínimo 6
  caracteres), perfil e lojas.
- Usuário ou e-mail repetido não é aceito.
- "Excluir" um usuário o **desativa**: ele não consegue mais entrar, nem no app, mas o histórico
  (vendas, log) continua. Dá para reativar depois.
- Cada pessoa pode trocar a própria senha [tela?: Meu perfil]; o administrador pode redefinir a
  senha de outra pessoa.
- Um administrador não consegue se desativar nem tirar o próprio perfil de administrador.

## Produtos e categorias [tela?: Produtos / Categorias]
- Produto pertence a uma categoria, e a categoria a um departamento.
- SKU e código de barras não podem se repetir na mesma loja.
- **Preço**: cada alteração cria um novo preço com data de início; o anterior é encerrado (o
  histórico de preços fica guardado). Não é possível criar um preço com início antes do preço
  vigente. Preço de oferta, quando maior que zero, é o que vale na venda.
- **Custo** do produto é usado para calcular a margem. A margem de uma venda usa o custo do
  momento da venda.
- "Excluir" produto o **inativa**: ele some das vendas, mas continua nos relatórios.
- "Excluir" categoria também a **inativa**, e só é possível quando ela não tem nenhum produto
  ativo (mova ou inative os produtos antes). Não dá para cadastrar produto em categoria inativa.

## Estoque [tela?: Estoque]
- O estoque muda por venda, cancelamento (devolve ao estoque) e ajustes manuais (reposição,
  perda, devolução, ajuste). Todo movimento fica registrado.
- Um ajuste que deixaria o estoque negativo é recusado.

## Pessoas e clientes [tela?: Clientes]
- Clientes, fornecedores e outros cadastros ficam todos em Pessoas; cada pessoa tem um ou mais
  papéis (ex.: cliente e fornecedor ao mesmo tempo) e pode ser pessoa física (CPF) ou
  jurídica (CNPJ).
- O CPF/CNPJ não pode se repetir dentro da mesma loja. "Excluir" uma pessoa a inativa;
  cliente inativo não pode comprar. Só quem tem o papel de cliente pode ser o comprador de
  uma venda.
- Vendedor cadastra e edita só clientes; cadastrar ou editar fornecedores (e outros papéis)
  exige perfil Gerente ou Administrador. Para tornar um cliente também fornecedor, edite os
  papéis da pessoa em vez de cadastrá-la de novo.
- O histórico de compras de um cliente aparece na ficha dele [tela?].

## Vendas [tela?: Vendas]
- Uma venda tem cliente, itens, forma de pagamento (dinheiro, débito, crédito, Pix, outro) e
  desconto opcional. O preço vem do cadastro — não é digitado na venda.
- A venda é recusada se faltar estoque, se o produto estiver inativo ou sem preço, se o cliente
  estiver inativo ou se o desconto passar do valor da venda.
- O desconto do pedido é distribuído entre os itens proporcionalmente; por isso o faturamento
  dos relatórios é sempre **líquido** (já sem descontos).
- Status: **pendente** → **paga** (dar baixa) ou **cancelada**. Cancelar devolve os itens ao
  estoque. Venda não é editada: para corrigir, cancele e registre de novo.

## Compras [tela?: Compras] (Gerente e Administrador)
- Uma compra tem fornecedor (pessoa com papel de fornecedor), itens com o **custo unitário da
  nota**, desconto e frete opcionais, número/chave da nota e data de emissão.
- Ela pode ser registrada **pendente** (pedido ao fornecedor) e **recebida** depois, ou já
  recebida. O estoque só entra no recebimento.
- Desconto e frete são distribuídos entre os itens proporcionalmente ao valor de cada um; o
  custo de cada produto passa a ser o **custo da última compra** (já com desconto e frete).
  Uma compra mais antiga lançada depois não substitui um custo mais recente.
- Cancelar uma compra recebida retira os itens do estoque — não é possível se eles já foram
  vendidos. O custo do produto não volta ao anterior (ajuste manualmente se precisar).
- No app, compras podem ser lançadas sem internet: entram como recebidas na sincronização.

## Vendas do app (offline) e sincronização
- O app registra vendas sem internet e as envia quando a conexão volta. A venda conta no dia
  em que aconteceu, com o preço daquele dia.
- No envio, uma venda pode ser **recusada** (por exemplo: o estoque acabou antes de ela chegar
  ao servidor, ou o cliente foi inativado). Ela aparece no app como recusada, com o motivo.

## Relatórios e dashboard [tela?: Dashboard / Relatórios]
- Dashboard: vendas de hoje, clientes novos, produtos ativos, valor do estoque e últimos 7 dias.
- Relatórios por período: resumo de vendas, por cliente, por status, por categoria, produtos
  mais vendidos, comparação com o período anterior, por hora, por dia da semana, por vendedor,
  por loja, margem, estoque baixo, produtos parados e clientes inativos.
- Os dias seguem o horário de Brasília.

## Análises e previsões [tela?: Análises / Previsões]
- As análises (evolução diária, ranking, faltas de estoque) usam **dias completos**: vão até
  ontem e são atualizadas toda madrugada.
- A **previsão** estima as vendas dos próximos dias (7 e 14) a partir do histórico, ignorando
  dias em que o produto estava sem estoque. É gerada toda segunda-feira.
- Toda previsão vem com o **erro típico** do modelo (medido comparando previsões antigas com o
  que de fato aconteceu). Ex.: erro de 12% quer dizer que, em média, o real fica 12% acima ou
  abaixo do previsto. A previsão por loja é bem mais precisa que a por produto.

## Log de auditoria [tela?: Auditoria] (só Administrador)
- Registra entradas no sistema (inclusive tentativas com senha errada) e toda alteração:
  quem fez, quando, de onde e o que mudou. Não registra consultas.

## Assistente IA e alertas do agente (plano Hulk)
- O chat e o agente de IA fazem parte do plano **Hulk**; no plano Pro aparece "Plano insuficiente".
- Toda madrugada o agente analisa a loja e gera **alertas** [tela?: Alertas] (só Administrador e
  Gerente veem): vendas fora do esperado pela previsão, produto que pode faltar (com sugestão de
  compra), produto que parou de vender, venda abaixo do custo, pico de cancelamentos e dados
  desatualizados. Cada alerta pode ser marcado como visto ou dispensado.
- Toda segunda-feira sai um **resumo da semana** escrito pela IA com os números da loja.
- No chat, a IA também atua como **consultora**: analisa vendas, margem, horários, produtos parados,
  estoque e clientes e sugere estratégias para vender mais. Quando a sugestão é algo que o sistema
  faz (criar ou encerrar oferta, mudar preço ou custo, criar compra pendente de reposição, tratar
  alertas), ela pergunta "Posso fazer isso? Responda sim ou não." e só executa se a resposta for
  sim, em até 15 minutos, com as permissões de quem respondeu. Ela nunca mexe em usuários, perfis,
  senhas, plano ou vendas.

## O que o assistente (este chat) faz e não faz
- Responde perguntas sobre vendas, estoque, previsões e sobre como usar o sistema.
- Não faz alterações: não registra vendas, não muda preços nem estoque — explica como fazer
  no painel.
