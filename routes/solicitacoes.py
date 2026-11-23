from datetime import datetime
import random

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from database import db
from database.models import (
    Agendamento,
    Medico,
    Paciente,
    PoliticaPrioridade,
    PrioridadeAgendamento,
    Servico,
    Solicitacao,
    Usuario,
)
from services.auditoria import (
    registrar_consulta_auditoria,
    registrar_evento_auditoria,
)
from services.permissions import has_profile_permission
from services.disponibilidade import (
    medicos_disponiveis_para_atendimento,
)
from services.prioridades import (
    PRIORITY_CATEGORIES,
    validate_priority_registration,
)
from services.senhas import criar_senha_para_agendamento

solicitacoes_bp = Blueprint("solicitacoes", __name__, template_folder="../templates")


def _require_permission():
    if not has_profile_permission(
        current_user,
        "recepcionista",
        {"gerenciar_solicitacoes", "acessar_dashboard"},
    ):
        flash("Você não tem permissão para gerenciar solicitações.", "error")
        return False
    return True


@solicitacoes_bp.route("/solicitacoes")
@login_required
def listar():
    if not _require_permission():
        return redirect(url_for("auth.dashboard"))

    solicitacoes = Solicitacao.query.order_by(Solicitacao.data_solicitacao.desc()).all()
    registrar_consulta_auditoria("solicitacao", solicitacoes)
    return render_template("solicitacoes/lista.html", solicitacoes=solicitacoes)


@solicitacoes_bp.route("/solicitacoes/nova", methods=["GET", "POST"])
@login_required
def nova():
    if not _require_permission():
        return redirect(url_for("auth.dashboard"))

    pacientes = Paciente.query.filter_by(ativo=True).order_by(Paciente.nome_completo.asc()).all()
    registrar_consulta_auditoria("paciente", pacientes)
    servicos = Servico.query.filter_by(ativo=True).order_by(Servico.nome.asc()).all()
    medicos = (
        Medico.query.join(Usuario, Medico.usuario_id == Usuario.id)
        .filter(
            Medico.ativo.is_(True),
            Medico.sala_id.isnot(None),
            Usuario.ativo.is_(True),
            Usuario.perfil == "medico",
        )
        .order_by(Medico.nome_exibicao.asc())
        .all()
    )
    medicos = [
        medico
        for medico in medicos
        if medico.sala and medico.sala.ativa and medico.sala.status != "indisponivel"
    ]
    form_context = {
        "solicitacao": None,
        "pacientes": pacientes,
        "servicos": servicos,
        "medicos": medicos,
    }
    if request.method == "POST":
        paciente_id = request.form.get("paciente_id", type=int)
        servico_id = request.form.get("servico_id", type=int)
        medico_id = request.form.get("medico_id", type=int)
        observacoes = request.form.get("observacoes", "").strip()

        servico = db.session.get(Servico, servico_id) if servico_id else None
        if not servico or not servico.ativo:
            flash("Selecione um serviço válido e ativo.", "error")
            return render_template(
                "solicitacoes/form.html",
                **form_context,
                form_data=request.form,
            )

        medico = next(
            (
                item
                for item in medicos
                if item.id == medico_id
                and item.especialidade_id == servico.especialidade_id
            ),
            None,
        )
        if not medico:
            flash(
                "Selecione um médico ativo da especialidade do serviço, "
                "com sala disponível.",
                "error",
            )
            return render_template(
                "solicitacoes/form.html",
                **form_context,
                form_data=request.form,
            )

        paciente = db.session.get(Paciente, paciente_id) if paciente_id else None
        if paciente_id and (not paciente or not paciente.ativo):
            flash("Selecione um paciente ativo.", "error")
            return render_template(
                "solicitacoes/form.html",
                **form_context,
                form_data=request.form,
            )

        novo_paciente = False
        if paciente is None:
            nome = request.form.get("novo_nome_completo", "").strip()
            cpf = Paciente.normalize_cpf(request.form.get("novo_cpf", ""))
            data_nascimento_texto = request.form.get("novo_data_nascimento", "").strip()
            telefone = request.form.get("novo_telefone", "").strip()
            endereco = request.form.get("novo_endereco", "").strip()
            observacoes_paciente = request.form.get("novo_observacoes", "").strip()
            try:
                data_nascimento = datetime.strptime(
                    data_nascimento_texto,
                    "%Y-%m-%d",
                ).date()
            except ValueError:
                data_nascimento = None

            if not nome or not cpf or not data_nascimento or not telefone or not endereco:
                flash("Preencha todos os dados obrigatórios do paciente.", "error")
                return render_template(
                    "solicitacoes/form.html",
                    **form_context,
                    form_data=request.form,
                )
            if len(cpf) != 11 or not cpf.isdigit():
                flash("CPF inválido. Informe 11 dígitos numéricos.", "error")
                return render_template(
                    "solicitacoes/form.html",
                    **form_context,
                    form_data=request.form,
                )
            if Paciente.query.filter_by(cpf=cpf).first():
                flash("Paciente já cadastrado com este CPF; selecione-o na lista.", "error")
                return render_template(
                    "solicitacoes/form.html",
                    **form_context,
                    form_data=request.form,
                )
            paciente = Paciente(
                nome_completo=nome,
                cpf=cpf,
                data_nascimento=data_nascimento,
                telefone=telefone,
                endereco=endereco,
                observacoes=observacoes_paciente or None,
                ativo=True,
            )
            db.session.add(paciente)
            db.session.flush()
            registrar_evento_auditoria("paciente", paciente.id, "criar")
            novo_paciente = True

        codigo = f"SL-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{random.randint(100, 999)}"

        solicitacao = Solicitacao(
            codigo=codigo,
            paciente_id=paciente.id,
            servico_id=servico.id,
            especialidade_id=servico.especialidade_id,
            medico_id=medico.id,
            valor_snapshot=servico.valor,
            status="aguardando_pagamento",
            observacoes=observacoes or None,
        )

        db.session.add(solicitacao)
        db.session.flush()
        registrar_evento_auditoria("solicitacao", solicitacao.id, "criar")
        db.session.commit()
        flash(
            "Paciente e atendimento registrados. Imprima o documento para o paciente levar ao setor financeiro."
            if novo_paciente
            else "Atendimento registrado. Imprima o documento para o paciente levar ao setor financeiro.",
            "info",
        )
        return redirect(
            url_for("solicitacoes.documento_financeiro", solicitacao_id=solicitacao.id)
        )

    return render_template(
        "solicitacoes/form.html",
        **form_context,
        form_data={},
    )


@solicitacoes_bp.route("/solicitacoes/<int:solicitacao_id>/documento-financeiro")
@login_required
def documento_financeiro(solicitacao_id):
    if not _require_permission():
        return redirect(url_for("auth.dashboard"))

    solicitacao = Solicitacao.query.get_or_404(solicitacao_id)
    registrar_consulta_auditoria("solicitacao", [solicitacao])
    db.session.commit()
    return render_template(
        "solicitacoes/documento_financeiro.html",
        solicitacao=solicitacao,
        data_emissao=datetime.now(),
    )


@solicitacoes_bp.route(
    "/solicitacoes/<int:solicitacao_id>/emitir-senha",
    methods=["GET", "POST"],
)
@login_required
def emitir_senha(solicitacao_id):
    if not _require_permission():
        return redirect(url_for("auth.dashboard"))
    if not has_profile_permission(
        current_user,
        "recepcionista",
        "gerenciar_fila",
    ):
        flash("Você não tem permissão para emitir senhas.", "error")
        return redirect(url_for("auth.dashboard"))

    solicitacao = Solicitacao.query.get_or_404(solicitacao_id)
    if not solicitacao.validacao_pagamento or solicitacao.status != "confirmado":
        flash("Registre primeiro a confirmação oficial do setor financeiro.", "error")
        return redirect(url_for("solicitacoes.listar"))

    if solicitacao.agendamento:
        if solicitacao.agendamento.senha_vinculo:
            return redirect(
                url_for(
                    "fila.imprimir",
                    senha_id=solicitacao.agendamento.senha_vinculo.senha_id,
                )
            )
        if solicitacao.agendamento.status != "cancelado":
            flash("Este atendimento já possui um agendamento.", "info")
            return redirect(
                url_for(
                    "agenda.listar",
                    data=solicitacao.agendamento.data_hora.date().isoformat(),
                )
            )
        flash(
            "Este atendimento já teve um agendamento cancelado. Use a agenda para organizar um novo horário.",
            "error",
        )
        return redirect(url_for("solicitacoes.listar"))

    agora = datetime.now()
    horario_atendimento, medicos = medicos_disponiveis_para_atendimento(
        solicitacao,
        agora,
        medico_id=solicitacao.medico_id,
    )
    politica_prioridade = db.session.get(PoliticaPrioridade, 1)

    if request.method == "POST":
        medico_id = request.form.get("medico_id", type=int)
        medico = next((item for item in medicos if item.id == medico_id), None)
        if horario_atendimento is None:
            erro = "Não há médicos ativos desta especialidade com sala disponível no momento."
        elif not medico:
            erro = "Selecione um médico ativo desta especialidade com sala disponível."
        else:
            erro = None

        categoria = request.form.get("categoria_prioridade", "").strip()
        prioridade = bool(categoria)
        prioridade_registrada = None
        data_doacao = None
        if prioridade and not erro:
            if (
                not politica_prioridade
                or not politica_prioridade.aprovada
                or categoria not in (politica_prioridade.categorias_habilitadas or [])
            ):
                erro = "A política ou a categoria de prioridade ainda não está aprovada pela direção."
            else:
                data_doacao_texto = request.form.get("data_doacao", "").strip()
                if data_doacao_texto:
                    try:
                        data_doacao = datetime.strptime(
                            data_doacao_texto,
                            "%Y-%m-%d",
                        ).date()
                    except ValueError:
                        data_doacao = None
                erro = validate_priority_registration(
                    categoria,
                    request.form.get("comprovacao_conferida") == "on",
                    data_doacao,
                    today=agora.date(),
                )
                if not erro:
                    prioridade_registrada = PrioridadeAgendamento(
                        categoria=categoria,
                        comprovacao_conferida=True,
                        data_doacao=data_doacao,
                        registrada_por_id=current_user.id,
                    )

        if erro:
            flash(erro, "error")
            return render_template(
                "solicitacoes/emitir_senha.html",
                solicitacao=solicitacao,
                medicos=medicos,
                horario_atendimento=horario_atendimento,
                politica_prioridade=politica_prioridade,
                categorias_prioridade=PRIORITY_CATEGORIES,
                form_data=request.form,
            )

        agendamento = Agendamento(
            solicitacao_id=solicitacao.id,
            paciente_id=solicitacao.paciente_id,
            medico_id=medico.id,
            sala_id=medico.sala_id,
            data_hora=horario_atendimento,
            status="agendado",
        )
        db.session.add(agendamento)
        db.session.flush()
        solicitacao.medico_id = medico.id
        solicitacao.status = "agendado"
        if prioridade_registrada:
            prioridade_registrada.agendamento_id = agendamento.id
            db.session.add(prioridade_registrada)
        senha = criar_senha_para_agendamento(
            agendamento,
            prioridade=prioridade,
            agora=agora,
        )
        registrar_evento_auditoria("agendamento", agendamento.id, "criar_atendimento_imediato")
        registrar_evento_auditoria("solicitacao", solicitacao.id, "agendar")
        registrar_evento_auditoria("senha", senha.id, "emitir")
        if prioridade:
            registrar_evento_auditoria(
                "prioridade_agendamento",
                agendamento.id,
                "registrar",
            )
        db.session.commit()
        flash(f"Senha {senha.codigo_exibicao} emitida para o paciente.", "info")
        return redirect(url_for("fila.imprimir", senha_id=senha.id))

    registrar_consulta_auditoria("solicitacao", [solicitacao])
    db.session.commit()
    return render_template(
        "solicitacoes/emitir_senha.html",
        solicitacao=solicitacao,
        medicos=medicos,
        horario_atendimento=horario_atendimento,
        politica_prioridade=politica_prioridade,
        categorias_prioridade=PRIORITY_CATEGORIES,
        form_data={},
    )


@solicitacoes_bp.route("/solicitacoes/<int:solicitacao_id>/confirmar-pagamento", methods=["GET", "POST"])
@login_required
def confirmar_pagamento(solicitacao_id):
    flash(
        "A confirmação de pagamentos está disponível somente no módulo Financeiro.",
        "error",
    )
    if has_profile_permission(current_user, "financeiro", "gerenciar_financeiro"):
        return redirect(url_for("financeiro.index"))
    return redirect(url_for("auth.dashboard"))
