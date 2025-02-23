"""Class to hold all light accessories."""
import logging

from pyhap.const import CATEGORY_LIGHTBULB

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT,
    ATTR_COLOR_TEMP,
    ATTR_HS_COLOR,
    ATTR_MAX_MIREDS,
    ATTR_MIN_MIREDS,
    ATTR_SUPPORTED_COLOR_MODES,
    COLOR_MODE_COLOR_TEMP,
    COLOR_MODES_BRIGHTNESS,
    COLOR_MODES_COLOR,
    DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import callback
from homeassistant.util.color import (
    color_temperature_mired_to_kelvin,
    color_temperature_to_hs,
)

from .accessories import TYPES, HomeAccessory
from .const import (
    CHAR_BRIGHTNESS,
    CHAR_COLOR_TEMPERATURE,
    CHAR_HUE,
    CHAR_ON,
    CHAR_SATURATION,
    PROP_MAX_VALUE,
    PROP_MIN_VALUE,
    SERV_LIGHTBULB,
)

_LOGGER = logging.getLogger(__name__)

RGB_COLOR = "rgb_color"


@TYPES.register("Light")
class Light(HomeAccessory):
    """Generate a Light accessory for a light entity.

    Currently supports: state, brightness, color temperature, rgb_color.
    """

    def __init__(self, *args):
        """Initialize a new Light accessory object."""
        super().__init__(*args, category=CATEGORY_LIGHTBULB)

        self.chars = []
        state = self.hass.states.get(self.entity_id)

        self._features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        self._color_modes = state.attributes.get(ATTR_SUPPORTED_COLOR_MODES, [])

        if any(mode in self._color_modes for mode in COLOR_MODES_BRIGHTNESS):
            self.chars.append(CHAR_BRIGHTNESS)

        if any(mode in self._color_modes for mode in COLOR_MODES_COLOR):
            self.chars.append(CHAR_HUE)
            self.chars.append(CHAR_SATURATION)
        elif COLOR_MODE_COLOR_TEMP in self._color_modes:
            # ColorTemperature and Hue characteristic should not be
            # exposed both. Both states are tracked separately in HomeKit,
            # causing "source of truth" problems.
            self.chars.append(CHAR_COLOR_TEMPERATURE)

        serv_light = self.add_preload_service(SERV_LIGHTBULB, self.chars)

        self.char_on = serv_light.configure_char(CHAR_ON, value=0)

        if CHAR_BRIGHTNESS in self.chars:
            # Initial value is set to 100 because 0 is a special value (off). 100 is
            # an arbitrary non-zero value. It is updated immediately by async_update_state
            # to set to the correct initial value.
            self.char_brightness = serv_light.configure_char(CHAR_BRIGHTNESS, value=100)

        if CHAR_COLOR_TEMPERATURE in self.chars:
            min_mireds = self.hass.states.get(self.entity_id).attributes.get(
                ATTR_MIN_MIREDS, 153
            )
            max_mireds = self.hass.states.get(self.entity_id).attributes.get(
                ATTR_MAX_MIREDS, 500
            )
            self.char_color_temperature = serv_light.configure_char(
                CHAR_COLOR_TEMPERATURE,
                value=min_mireds,
                properties={PROP_MIN_VALUE: min_mireds, PROP_MAX_VALUE: max_mireds},
            )

        if CHAR_HUE in self.chars:
            self.char_hue = serv_light.configure_char(CHAR_HUE, value=0)

        if CHAR_SATURATION in self.chars:
            self.char_saturation = serv_light.configure_char(CHAR_SATURATION, value=75)

        self.async_update_state(state)

        serv_light.setter_callback = self._set_chars

    def _set_chars(self, char_values):
        _LOGGER.debug("Light _set_chars: %s", char_values)
        events = []
        service = SERVICE_TURN_ON
        params = {ATTR_ENTITY_ID: self.entity_id}
        if CHAR_ON in char_values:
            if not char_values[CHAR_ON]:
                service = SERVICE_TURN_OFF
            events.append(f"Set state to {char_values[CHAR_ON]}")

        if CHAR_BRIGHTNESS in char_values:
            if char_values[CHAR_BRIGHTNESS] == 0:
                events[-1] = "Set state to 0"
                service = SERVICE_TURN_OFF
            else:
                params[ATTR_BRIGHTNESS_PCT] = char_values[CHAR_BRIGHTNESS]
            events.append(f"brightness at {char_values[CHAR_BRIGHTNESS]}%")

        if CHAR_COLOR_TEMPERATURE in char_values:
            params[ATTR_COLOR_TEMP] = char_values[CHAR_COLOR_TEMPERATURE]
            events.append(f"color temperature at {char_values[CHAR_COLOR_TEMPERATURE]}")

        if (
            any(mode in self._color_modes for mode in COLOR_MODES_COLOR)
            and CHAR_HUE in char_values
            and CHAR_SATURATION in char_values
        ):
            color = (char_values[CHAR_HUE], char_values[CHAR_SATURATION])
            _LOGGER.debug("%s: Set hs_color to %s", self.entity_id, color)
            params[ATTR_HS_COLOR] = color
            events.append(f"set color at {color}")

        self.async_call_service(DOMAIN, service, params, ", ".join(events))

    @callback
    def async_update_state(self, new_state):
        """Update light after state change."""
        # Handle State
        state = new_state.state
        if state == STATE_ON and self.char_on.value != 1:
            self.char_on.set_value(1)
        elif state == STATE_OFF and self.char_on.value != 0:
            self.char_on.set_value(0)

        # Handle Brightness
        if CHAR_BRIGHTNESS in self.chars:
            brightness = new_state.attributes.get(ATTR_BRIGHTNESS)
            if isinstance(brightness, (int, float)):
                brightness = round(brightness / 255 * 100, 0)
                # The homeassistant component might report its brightness as 0 but is
                # not off. But 0 is a special value in homekit. When you turn on a
                # homekit accessory it will try to restore the last brightness state
                # which will be the last value saved by char_brightness.set_value.
                # But if it is set to 0, HomeKit will update the brightness to 100 as
                # it thinks 0 is off.
                #
                # Therefore, if the the brightness is 0 and the device is still on,
                # the brightness is mapped to 1 otherwise the update is ignored in
                # order to avoid this incorrect behavior.
                if brightness == 0 and state == STATE_ON:
                    brightness = 1
                if self.char_brightness.value != brightness:
                    self.char_brightness.set_value(brightness)

        # Handle color temperature
        if CHAR_COLOR_TEMPERATURE in self.chars:
            color_temperature = new_state.attributes.get(ATTR_COLOR_TEMP)
            if isinstance(color_temperature, (int, float)):
                color_temperature = round(color_temperature, 0)
                if self.char_color_temperature.value != color_temperature:
                    self.char_color_temperature.set_value(color_temperature)

        # Handle Color
        if CHAR_SATURATION in self.chars and CHAR_HUE in self.chars:
            if ATTR_HS_COLOR in new_state.attributes:
                hue, saturation = new_state.attributes[ATTR_HS_COLOR]
            elif ATTR_COLOR_TEMP in new_state.attributes:
                hue, saturation = color_temperature_to_hs(
                    color_temperature_mired_to_kelvin(
                        new_state.attributes[ATTR_COLOR_TEMP]
                    )
                )
            else:
                hue, saturation = None, None
            if isinstance(hue, (int, float)) and isinstance(saturation, (int, float)):
                hue = round(hue, 0)
                saturation = round(saturation, 0)
                if hue != self.char_hue.value:
                    self.char_hue.set_value(hue)
                if saturation != self.char_saturation.value:
                    self.char_saturation.set_value(saturation)
