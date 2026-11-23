from datetime import datetime

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from database import db
from database.models import Atendimento, HistoricoStatus, Prontuario
from services.auditoria import (
    registrar_consulta_auditoria,
    registrar_evento_auditoria,
)
from services.permissions import has_profile_permission
from services.senhas import concluir_fluxo_fila

atendimento_bp = Blueprint("atendimento", __name__, template_folder="../templates")

PRONTUARIO_FIELDS = (
    "queixa_principal",
    "sintomas",
    "diagnostico",
    "prescricao",
    "exames_solicitados",
    "condutas",
    "orientacoes",
)


def _medico_autorizado():
    medico = current_user.medico
    if (
        not has_profile_permission(
            current_user,
            "medico",
            "gerenciar_atendimento",
        )
        or not current_user.ativo
        or medico is None
        or not medico.ativo
    ):
        abort(403)
    return medico


def _historico_prontuarios(atendimento):
    return (
        Prontuario.query.join(Atendimento)
        .filter(
            Atendimento.paciente_id == atendimento.paciente_id,
            Atendimento.id != atendimento.id,
            Atendimento.status.in_({"finalizado", "concluido"}),
        )
        .order_by(Atendimento.data_fim.desc(), Prontuario.criado_em.desc())
        .all()
    )


def _render_prontuario(atendimento, prontuario, form_data):
    historico = _historico_prontuarios(atendimento)
    registrar_evento_auditoria(
        "atendimento",
        atendimento.id,
        "consultar_prontuario",
    )
    if prontuario:
        registrar_evento_auditoria("prontuario", prontuario.id, "consultar")
    for registro in historico:
        registrar_evento_auditoria("prontuario", registro.id, "consultar")
    db.session.commit()
    return render_template(
        "atendimento/prontuario.html",
        atendimento=atendimento,
        prontuario=prontuario,
        historico_prontuarios=historico,
        form_data=form_data,
    )


@atendimento_bp.route(
    "/atendimento/<int:atendimento_id>",
    methods=["GET", "POST"],
)
@login_required
def prontuario(atendimento_id):
    medico = _medico_autorizado()
    atendimento = Atendimento.query.get_or_404(atendimento_id)
    if atendimento.medico_id != medico.id:
        abort(403)

    prontuario = atendimento.prontuario
    if atendimento.status not in {"em_andamento", "finalizado", "concluido"}:
        abort(404)
    if atendimento.status in {"finalizado", "concluido"} and not prontuario:
        abort(404)
    if request.method == "POST":
        if atendimento.status != "em_andamento":
            abort(409)
        if atendimento.senha and atendimento.senha.status not in {
            "chamada",
            "em_atendimento",
        }:
            flash(
                "A senha não está mais em atendimento; o prontuário não foi alterado.",
                "error",
            )
            return redirect(url_for("fila.listar"))

        form_data = {
            field: request.form.get(field, "").strip()
            for field in PRONTUARIO_FIELDS
        }
        if not form_data["queixa_principal"] or not form_data["diagnostico"]:
            flash("Queixa principal e diagnóstico são obrigatórios.", "error")
            return _render_prontuario(atendimento, prontuario, form_data)

        criando = prontuario is None
        if criando:
            prontuario = Prontuario(atendimento_id=atendimento.id)
        for field in PRONTUARIO_FIELDS:
            setattr(prontuario, field, form_data[field] or None)
        if criando:
            db.session.add(prontuario)
            db.session.flush()

        status_anterior = atendimento.status
        atendimento.status = "finalizado"
        atendimento.data_fim = datetime.utcnow()
        db.session.add(
            HistoricoStatus(
                entidade="atendimento",
                entidade_id=atendimento.id,
                status_anterior=status_anterior,
                status_novo=atendimento.status,
                alterado_por_id=current_user.id,
            )
        )
        registrar_evento_auditoria(
            "prontuario",
            prontuario.id,
            "criar_prontuario" if criando else "alterar_prontuario",
        )
        registrar_evento_auditoria(
            "atendimento",
            atendimento.id,
            "finalizar_atendimento",
        )
        if atendimento.senha:
            concluir_fluxo_fila(atendimento.senha, current_user.id)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            current_app.logger.exception(
                "Unable to finalize medical record for attendance %s.",
                atendimento_id,
            )
            flash(
                "Não foi possível salvar o prontuário. Atualize a tela e tente novamente.",
                "error",
            )
            return redirect(url_for("fila.listar"))

        flash("Prontuário salvo e atendimento finalizado.", "info")
        return redirect(
            url_for("atendimento.prontuario", atendimento_id=atendimento.id)
        )

    form_data = {
        field: getattr(prontuario, field, None) or ""
        for field in PRONTUARIO_FIELDS
    }
    return _render_prontuario(atendimento, prontuario, form_data)


@atendimento_bp.route("/prontuarios")
@login_required
def listar():
    medico = _medico_autorizado()
    atendimentos = (
        Atendimento.query.filter_by(medico_id=medico.id)
        .order_by(Atendimento.data_inicio.desc())
        .all()
    )
    registrar_consulta_auditoria("atendimento", atendimentos)
    return render_template(
        "atendimento/lista.html",
        atendimentos=atendimentos,
    )


@atendimento_bp.route("/atendimento/<int:atendimento_id>/imprimir")
@login_required
def imprimir(atendimento_id):
    medico = _medico_autorizado()
    atendimento = Atendimento.query.get_or_404(atendimento_id)
    prontuario = atendimento.prontuario
    if not prontuario:
        abort(404)

    if atendimento.medico_id != medico.id:
        contexto_id = request.args.get("contexto", type=int)
        contexto = (
            db.session.get(Atendimento, contexto_id)
            if contexto_id
            else None
        )
        if (
            not contexto
            or contexto.paciente_id != atendimento.paciente_id
            or contexto.medico_id != medico.id
            or contexto.status not in {"em_andamento", "finalizado", "concluido"}
        ):
            abort(403)

    registrar_evento_auditoria("prontuario", prontuario.id, "imprimir_prontuario")
    db.session.commit()
    return render_template(
        "atendimento/imprimir.html",
        atendimento=atendimento,
        prontuario=prontuario,
        data_emissao=datetime.now(),
    )
