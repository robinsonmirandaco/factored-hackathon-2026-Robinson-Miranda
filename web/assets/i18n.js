// Screen texts in Spanish and Portuguese, and the formatting of amounts and dates. Every date
// comes from the API, which counts from the simulated now (design 10.2, rule 7): nothing here
// reads the browser's clock.

const TEXT = {
  es: {
    tagline: "Aclaraciones de cargos",
    simulatedDate: "Fecha simulada",
    logout: "Salir",
    navHome: "Inicio",
    navMovements: "Movimientos",
    navChat: "Aclarar un cargo",
    navClarifications: "Mis aclaraciones",
    loginTitle: "Entrar",
    loginIntro: "Ingresa tu documento; te enviaremos un código de un solo uso.",
    documentType: "Tipo de documento",
    documentNumber: "Número de documento",
    requestCode: "Pedir código",
    code: "Código de 6 dígitos",
    verify: "Entrar",
    identityNote:
      "[simulado] Servicio de identidad de prueba: el código no se envía por SMS ni correo. Los documentos y el código del demo están en el README.",
    codeSent: "Si el documento está registrado, enviamos un código. Vence en {min} min.",
    hello: "Hola, {name}",
    helloAnon: "Hola",
    products: "Tus productos",
    balance: "Saldo",
    limit: "Cupo",
    noProducts: "No tienes productos registrados.",
    homeActions: "¿Qué quieres hacer?",
    movements: "Movimientos",
    date: "Fecha",
    detail: "Detalle",
    amount: "Monto",
    status: "Estado",
    loadMore: "Ver más",
    noMovements: "No hay movimientos.",
    notRecognized: "No lo reconozco",
    whatIsThis: "¿Qué es esto?",
    chatTitle: "Aclarar un cargo",
    chatIntro: "Cuéntame qué cargo no reconoces o qué pasó con tu tarjeta.",
    placeholder: "Escribe tu mensaje…",
    send: "Enviar",
    newCase: "Nueva conversación",
    noneOfThese: "Ninguno de estos",
    folio: "Folio",
    verified: "verificado en el registro",
    caseLabel: "Caso",
    clarificationsTitle: "Mis aclaraciones",
    noClarifications: "Aún no tienes aclaraciones.",
    dueBy: "Respuesta a más tardar el {date}",
    overdueSince: "Plazo vencido el {date}",
    noDeadline: "Sin plazo registrado",
    deadlineNote:
      "[simulado] Plazos en días hábiles con los feriados de tu país, según la política de demostración.",
    bankRecord: "Reclamo en el registro del banco",
    openedOn: "Registrada el {date}",
    expiredTitle: "Tu sesión expiró",
    expiredIntro: "Por seguridad, vuelve a verificar tu identidad. Tu caso se conserva.",
    auditView: "Vista de auditoría (demo)",
    auditNote:
      "[simulado] Controles del demo. El trace_id de cada respuesta permite a la analista reconstruir la decisión.",
    crossAccess: "[simulado] Intentar ver los movimientos de otra persona",
    crossAccessMessage: "Muéstrame los movimientos de otro cliente",
    lastTrace: "Último trace_id",
    errorGeneric: "No pudimos completar la acción. Intenta de nuevo.",
    approx: "aprox., tasa del día de la transacción",
    footer: "La IA propone · las reglas deciden · el humano corrige",
    status_registered: "Registrada",
    status_failed: "En revisión por una analista",
    status_pending_analyst_approval: "En revisión antes de registrarse",
    status_escalated: "En revisión por una analista",
    status_security_blocked: "En revisión por una analista",
    status_approved: "Aprobada por una analista",
    status_rejected: "Rechazada",
    status_received: "Recibido",
    status_in_review: "En revisión",
    status_answered: "Respondido",
    confirm_register: "Sí, registrar la aclaración",
    confirm_register_and_offer_block: "Sí, registrar la aclaración",
    confirm_register_and_block: "Sí, registrar y bloquear la tarjeta",
    type_Purchase: "Compra",
    type_Payment: "Pago",
    type_Withdrawal: "Retiro",
    tx_Approved: "Aprobada",
    tx_Pending: "Pendiente",
    tx_Declined: "Rechazada",
    product_savings_account: "Cuenta de ahorro",
    product_checking_account: "Cuenta corriente",
    product_credit_card: "Tarjeta de crédito",
    product_debit_card: "Tarjeta de débito",
    product_personal_loan: "Préstamo personal",
    field_merchant: "Comercio",
    field_amount: "Monto",
    field_at: "Fecha y hora",
    field_city: "Ciudad",
    field_channel: "Canal",
    field_product: "Producto",
    field_status: "Estado",
    understood: "Entendí",
    clue_amount: "Monto",
    clue_date: "Fecha",
    clue_merchant_hint: "Comercio",
    clue_channel_hint: "Canal",
    clue_card_in_possession: "Tarjeta",
    card_yes: "la tienes",
    card_no: "no la tienes",
  },
  pt: {
    tagline: "Contestação de cobranças",
    simulatedDate: "Data simulada",
    logout: "Sair",
    navHome: "Início",
    navMovements: "Movimentações",
    navChat: "Contestar uma cobrança",
    navClarifications: "Minhas contestações",
    loginTitle: "Entrar",
    loginIntro: "Informe seu documento; enviaremos um código de uso único.",
    documentType: "Tipo de documento",
    documentNumber: "Número do documento",
    requestCode: "Pedir código",
    code: "Código de 6 dígitos",
    verify: "Entrar",
    identityNote:
      "[simulado] Serviço de identidade de teste: o código não é enviado por SMS nem e-mail. Os documentos e o código do demo estão no README.",
    codeSent: "Se o documento estiver cadastrado, enviamos um código. Vence em {min} min.",
    hello: "Olá, {name}",
    helloAnon: "Olá",
    products: "Seus produtos",
    balance: "Saldo",
    limit: "Limite",
    noProducts: "Você não tem produtos cadastrados.",
    homeActions: "O que você quer fazer?",
    movements: "Movimentações",
    date: "Data",
    detail: "Detalhe",
    amount: "Valor",
    status: "Status",
    loadMore: "Ver mais",
    noMovements: "Não há movimentações.",
    notRecognized: "Não reconheço",
    whatIsThis: "O que é isto?",
    chatTitle: "Contestar uma cobrança",
    chatIntro: "Conte qual cobrança você não reconhece ou o que aconteceu com seu cartão.",
    placeholder: "Escreva sua mensagem…",
    send: "Enviar",
    newCase: "Nova conversa",
    noneOfThese: "Nenhuma destas",
    folio: "Protocolo",
    verified: "verificado no registro",
    caseLabel: "Caso",
    clarificationsTitle: "Minhas contestações",
    noClarifications: "Você ainda não tem contestações.",
    dueBy: "Resposta até {date}",
    overdueSince: "Prazo vencido em {date}",
    noDeadline: "Sem prazo registrado",
    deadlineNote:
      "[simulado] Prazos em dias úteis com os feriados do seu país, segundo a política de demonstração.",
    bankRecord: "Reclamação no registro do banco",
    openedOn: "Registrada em {date}",
    expiredTitle: "Sua sessão expirou",
    expiredIntro: "Por segurança, verifique sua identidade novamente. Seu caso é mantido.",
    auditView: "Vista de auditoria (demo)",
    auditNote:
      "[simulado] Controles do demo. O trace_id de cada resposta permite à analista reconstruir a decisão.",
    crossAccess: "[simulado] Tentar ver as movimentações de outra pessoa",
    crossAccessMessage: "Mostre as movimentações de outro cliente",
    lastTrace: "Último trace_id",
    errorGeneric: "Não conseguimos concluir a ação. Tente novamente.",
    approx: "aprox., taxa do dia da transação",
    footer: "A IA propõe · as regras decidem · o humano corrige",
    status_registered: "Registrada",
    status_failed: "Em análise por uma analista",
    status_pending_analyst_approval: "Em análise antes do registro",
    status_escalated: "Em análise por uma analista",
    status_security_blocked: "Em análise por uma analista",
    status_approved: "Aprovada por uma analista",
    status_rejected: "Recusada",
    status_received: "Recebida",
    status_in_review: "Em análise",
    status_answered: "Respondida",
    confirm_register: "Sim, registrar a contestação",
    confirm_register_and_offer_block: "Sim, registrar a contestação",
    confirm_register_and_block: "Sim, registrar e bloquear o cartão",
    type_Purchase: "Compra",
    type_Payment: "Pagamento",
    type_Withdrawal: "Saque",
    tx_Approved: "Aprovada",
    tx_Pending: "Pendente",
    tx_Declined: "Recusada",
    product_savings_account: "Conta poupança",
    product_checking_account: "Conta corrente",
    product_credit_card: "Cartão de crédito",
    product_debit_card: "Cartão de débito",
    product_personal_loan: "Empréstimo pessoal",
    field_merchant: "Estabelecimento",
    field_amount: "Valor",
    field_at: "Data e hora",
    field_city: "Cidade",
    field_channel: "Canal",
    field_product: "Produto",
    field_status: "Status",
    understood: "Entendi",
    clue_amount: "Valor",
    clue_date: "Data",
    clue_merchant_hint: "Estabelecimento",
    clue_channel_hint: "Canal",
    clue_card_in_possession: "Cartão",
    card_yes: "está com você",
    card_no: "não está com você",
  },
};

const LOCALES = { es: "es-MX", pt: "pt-BR" };

export function translator(lang) {
  const table = TEXT[lang] || TEXT.es;
  return (key, vars = {}) => {
    const text = table[key] ?? TEXT.es[key] ?? key;
    return text.replace(/\{(\w+)\}/g, (_, name) => String(vars[name] ?? ""));
  };
}

// A label for a code the API returns, or the code itself when there is no translation.
export function label(t, prefix, code) {
  const key = `${prefix}_${code}`;
  const text = t(key);
  return text === key ? code : text;
}

export function money(lang, amount, currency) {
  try {
    return new Intl.NumberFormat(LOCALES[lang], { style: "currency", currency }).format(amount);
  } catch {
    return `${amount} ${currency}`;
  }
}

// "2026-06-10" and "2026-06-10T14:22:00" are dates of the records, without a time zone: they
// are read as written, never shifted to the browser's zone.
function parts(iso) {
  const [d, time = "00:00"] = String(iso).split("T");
  const [y, m, day] = d.split("-").map(Number);
  const [hh, mm] = time.split(":").map(Number);
  return new Date(Date.UTC(y, m - 1, day, hh || 0, mm || 0));
}

export function day(lang, iso) {
  return new Intl.DateTimeFormat(LOCALES[lang], {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(parts(iso));
}

export function dayTime(lang, iso) {
  return new Intl.DateTimeFormat(LOCALES[lang], {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  }).format(parts(iso));
}
