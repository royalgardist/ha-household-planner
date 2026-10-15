"""Explicit HA user grants and a verified, version-pinned Donetick connection."""
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
from .donetick import Donetick, VERSION, VERIFIED_BUILD, credential_provider
from .engine import HouseholdError

DAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')

class HouseholdConfigFlow(config_entries.ConfigFlow, domain='household_planner'):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HouseholdOptionsFlow()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id('household_planner')
            self._abort_if_unique_id_configured()
            try:
                uid = user_input['manager_id'].strip()
                user = await self.hass.auth.async_get_user(uid)
                if not user or not user.is_active or not user.is_admin or user.system_generated:
                    raise HouseholdError('invalid_manager')
                client = Donetick(user_input['base_url'], credential_provider(user_input['api_key']), required_build=VERIFIED_BUILD)
                await self.hass.async_add_executor_job(client.verify_runtime)
                # Validate actual credentials/read contract as well as the health version.
                await self.hass.async_add_executor_job(client.read)
                data = {k: user_input[k] for k in ('base_url', 'api_key', 'reader_ids', 'writer_ids', 'no_sunday_vacuum')}
                data.update(manager_ids=uid, expected_version=VERSION, budgets=[user_input[d] for d in DAYS])
                return self.async_create_entry(title=user_input['name'], data=data)
            except HouseholdError as exc:
                errors['base'] = 'invalid_manager' if str(exc) == 'invalid_manager' else 'cannot_connect'
        fields = {
            vol.Required('name', default='Household'): str,
            vol.Required('base_url'): str,
            vol.Required('api_key'): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Required('manager_id', default=self.context.get('user_id', '')): str,
            vol.Optional('reader_ids', default=''): str,
            vol.Optional('writer_ids', default=''): str,
            vol.Required('no_sunday_vacuum', default=True): bool,
        }
        fields.update({vol.Required(day, default=8 if day != 'saturday' else 16): vol.All(int, vol.Range(min=0,max=100)) for day in DAYS})
        return self.async_show_form(step_id='user', errors=errors, data_schema=vol.Schema(fields))


class HouseholdOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required("gentle_smoothing", default=current.get("gentle_smoothing", False)): bool,
            vol.Required("no_sunday_vacuum", default=current.get("no_sunday_vacuum", self.config_entry.data.get("no_sunday_vacuum", True))): bool,
        }))
