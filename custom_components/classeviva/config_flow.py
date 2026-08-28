"""Config flow (interfaccia grafica) per ClasseViva."""
from __future__ import annotations

from datetime import date

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import ClasseVivaApi, ClasseVivaAuthError, ClasseVivaApiError, parse_anno
from .const import ANNO_ULTIMO, CONF_ANNO, CONF_PASSWORD, CONF_USERNAME, DOMAIN

# Quanti anni scolastici passati proporre nella tendina.
ANNI_PROPOSTI = 5


def _anni_disponibili() -> list[str]:
    """Elenco per la tendina: "ultimo" e gli ultimi anni scolastici.

    L'anno scolastico che inizia a settembre: prima di settembre l'anno "in
    corso" è ancora quello iniziato l'anno solare precedente.
    """
    oggi = date.today()
    inizio = oggi.year if oggi.month >= 9 else oggi.year - 1
    return [ANNO_ULTIMO] + [
        f"{a % 100:02d}/{(a + 1) % 100:02d}"
        for a in range(inizio, inizio - ANNI_PROPOSTI, -1)
    ]


def _schema(valori: dict | None = None) -> vol.Schema:
    valori = valori or {}
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=valori.get(CONF_USERNAME, "")): TextSelector(),
            vol.Required(CONF_PASSWORD, default=""): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            # custom_value: oltre alle voci proposte si può scrivere l'anno a mano
            # ("25", "25/26", "2025/2026" vanno tutti bene).
            vol.Optional(CONF_ANNO, default=valori.get(CONF_ANNO, ANNO_ULTIMO)): SelectSelector(
                SelectSelectorConfig(
                    options=_anni_disponibili(),
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class ClasseVivaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Setup guidato: chiede credenziali e anno scolastico, poi verifica il login."""

    VERSION = 2

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                anno = parse_anno(user_input.get(CONF_ANNO))
            except ValueError:
                errors[CONF_ANNO] = "anno_non_valido"
            else:
                api = ClasseVivaApi(
                    async_get_clientsession(self.hass),
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    anno,
                )
                try:
                    await api.login()
                except ClasseVivaAuthError:
                    errors["base"] = "invalid_auth"
                except ClasseVivaApiError:
                    errors["base"] = "cannot_connect"
                else:
                    # Lo stesso studente può essere aggiunto più volte, purché su
                    # anni scolastici diversi: l'anno entra quindi nell'unique_id.
                    unico = api.student_id
                    if anno is not None:
                        unico = f"{unico}_{anno}"
                    await self.async_set_unique_id(unico)
                    self._abort_if_unique_id_configured()

                    etichetta = (
                        f" {anno % 100:02d}/{(anno + 1) % 100:02d}"
                        if anno is not None else ""
                    )
                    return self.async_create_entry(
                        title=f"ClasseViva - {api.first_name}{etichetta}",
                        data={
                            CONF_USERNAME: user_input[CONF_USERNAME],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_ANNO: anno,
                        },
                    )

        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input), errors=errors
        )
