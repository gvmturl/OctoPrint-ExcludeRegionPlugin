# coding=utf-8
"""Module providing the GcodeHandlers class."""

# Potential future improvements:
#
# - Implement: G5  - Bezier curve
#      G5 [E<pos>] I<pos> J<pos> P<pos> Q<pos> X<pos> Y<pos>
#

from __future__ import absolute_import

import re
import math

from .RetractionState import RetractionState
from .AtCommandAction import ENABLE_EXCLUSION, DISABLE_EXCLUSION

REGEX_FLOAT_PATTERN = "[-+]?[0-9]*\\.?[0-9]+"
REGEX_FLOAT_ARG = re.compile("^(?P<label>[A-Za-z])\\s*(?P<value>%s)" % REGEX_FLOAT_PATTERN)
REGEX_SPLIT = re.compile("(?<!^)\\s*(?=[A-Za-z])")

INCHES_PER_MM = 25.4

MM_PER_ARC_SEGMENT = 1
TWO_PI = 2 * math.pi


class GcodeHandlers(object):
    """
    Maintains the position state and processes Gcode exclusion/manipulation.

    Attributes
    ----------
    _logger : Logger
        Logger for outputting log messages.
    state : ExcludeRegionState
        The plugin state object
    """

    def __init__(self, state, logger):
        """
        Initialize the instance properties.

        Parameters
        ----------
        state : ExcludeRegionState
            The plugin state object
        logger : Logger
            Logger for outputting log messages.
        """
        assert state is not None, "A state must be provided"
        assert logger is not None, "A logger must be provided"
        self.state = state
        self._logger = logger

    def planArc(self, endX, endY, i, j, clockwise):  # pylint: disable=too-many-locals,invalid-name
        """
        Compute a sequence of moves approximating an arc (G2/G3).

        This code is based on the arc planning logic in Marlin.

        Parameters
        ----------
        endX : float
            The final x axis position for the tool after the arc is processed
        endY : float
            The final y axis position for the tool after the arc is processed
        i : float
            Offset from the initial x axis position to the center point of the arc
        j : float
            Offset from the initial y axis position to the center point of the arc
        clockwise : boolean
            Whether this is a clockwise (G2) or counter-clockwise (G3) arc.

        Returns
        -------
        List of x,y pairs
            List containing an even number of float values describing coordinate pairs that
            approximate the arc.  Each value at an even index (0, 2, 4, etc) is an x coordinate,
            and each odd indexed value is a y coordinate.  The first point is comprised of the
            x value at index 0 and the y value at index 1, and so on.
        """
        x = self.state.position.X_AXIS.current
        y = self.state.position.Y_AXIS.current

        radius = math.hypot(i, j)

        # CCW angle of rotation between position and target from the circle center.
        centerX = x + i
        centerY = y + j
        rtX = endX - centerX
        rtY = endY - centerY
        angularTravel = math.atan2(-i * rtY + j * rtX, -i * rtX - j * rtY)
        if (angularTravel < 0):
            angularTravel += TWO_PI
        if (clockwise):
            angularTravel -= TWO_PI

        # Make a circle if the angular rotation is 0 and the target is current position
        if (angularTravel == 0) and (x == endX) and (y == endY):
            angularTravel = TWO_PI

        # Compute the number of segments based on the length of the arc
        arcLength = angularTravel * radius
        numSegments = int(min(math.ceil(arcLength / MM_PER_ARC_SEGMENT), 2))

        # TODO: verify this
        angle = math.atan2(-i, -j)
        angularIncrement = angularTravel / (numSegments - 1)

        rval = []
        for dummy in range(1, numSegments):
            angle += angularIncrement
            rval += [centerX + math.cos(angle) * radius, centerY + math.sin(angle) * radius]

        rval += [endX, endY]

        self._logger.debug(
            "planArc(endX=%s, endY=%s, i=%s, j=%s, clockwise=%s) = %s",
            endX, endY, i, j, clockwise, rval
        )

        return rval

    def handleGcode(self, cmd, gcode, subcode=None):
        """
        Inspects the provided gcode command and performs any necessary processing.

        Parameters
        ----------
        cmd : string
            The full Gcode command, including arguments.
        gcode : string
            Gcode command code only, e.g. G0 or M110
        subcode : string | None
            Subcode of the GCODE command, e.g. 1 for M80.1.

        Returns
        -------
        None | List of Gcode commands | IGNORE_GCODE_CMD
            If the command should be processed normally, returns None, otherwise returns one or
            more Gcode commands to execute instead or IGNORE_GCODE_CMD to prevent processing.
        """
        self.state.numCommands += 1
        method = getattr(self, "_handle_" + gcode, self.state.processExtendedGcode)
        return method(cmd, gcode, subcode)

    def _handle_G0(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """
        G0 - Linear Move (by convention: G0 is used when not extruding).

        G0 [E<pos>] [F<rate>] [X<pos>] [Y<pos>] [Z<pos>]
          E - amount to extrude while moving
          F - feed rate to accelerate to while moving
        """
        extruderPosition = None
        feedRate = None
        x = None
        y = None
        z = None
        cmdArgs = REGEX_SPLIT.split(cmd)
        for index in range(1, len(cmdArgs)):
            match = REGEX_FLOAT_ARG.search(cmdArgs[index])
            if (match is not None):
                label = match.group("label").upper()
                value = float(match.group("value"))
                if (label == "E"):
                    extruderPosition = value
                elif (label == "F"):
                    feedRate = value
                elif (label == "X"):
                    x = value
                elif (label == "Y"):
                    y = value
                elif (label == "Z"):
                    z = value

        return self.state.processLinearMoves(cmd, extruderPosition, feedRate, z, x, y)

    def _handle_G1(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """
        G1 - Linear Move (by convention: G1 is used when extruding).

        G1 [E<pos>] [F<rate>] [X<pos>] [Y<pos>] [Z<pos>]
          E - amount to extrude while moving
          F - feed rate to accelerate to while moving
        """
        return self._handle_G0(cmd, gcode, subcode)

    def _computeCenterOffsets(self, x, y, radius, clockwise):  # pylint: disable=too-many-locals
        """
        Compute the i & j offsets for an arc given a radius, direction and ending point.

        Parameters
        ----------
        x : float
            The ending X coordinate provided in the GCode command.
        y : float
            The ending Y coordinate provided in the GCode command.
        radius : float
            The radius of the arc to compute the center point offset for.
        clockwise : boolean
            Whether the arc proceeds in a clockwise or counter-clockwise direction.
        """
        # pylint: disable=invalid-name
        position = self.state.position
        i = 0
        j = 0
        p1 = position.X_AXIS.current
        q1 = position.Y_AXIS.current
        p2 = x
        q2 = y

        if (radius and (p1 != p2 or q1 != q2)):
            e = (1 if clockwise else 0) ^ (-1 if (radius < 0) else 1)
            deltaX = p2 - p1
            deltaY = q2 - q1
            dist = math.hypot(deltaX, deltaY)
            halfDist = dist / 2
            h = math.sqrt(radius*radius - halfDist*halfDist)
            midX = (p1 + p2) / 2
            midY = (q1 + q2) / 2
            sx = -deltaY / dist
            sy = -deltaX / dist
            centerX = midX + e * h * sx
            centerY = midY + e * h * sy

            i = centerX - p1
            j = centerY - q1

        return (i, j)

    def _handle_G2(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """
        G2 - Controlled Arc Move (Clockwise).

        G2 [E<pos>] [F<rate>] R<radius> [X<pos>] [Y<pos>] [Z<pos>]
        G2 [E<pos>] [F<rate>] I<offset> J<offset> [X<pos>] [Y<pos>] [Z<pos>]
        """
        # pylint: disable=invalid-name, too-many-locals
        clockwise = (gcode == "G2")
        position = self.state.position

        extruderPosition = None
        feedRate = None
        x = position.X_AXIS.current
        y = position.Y_AXIS.current
        z = position.Z_AXIS.current
        radius = None
        i = 0
        j = 0
        cmdArgs = REGEX_SPLIT.split(cmd)
        for index in range(1, len(cmdArgs)):
            match = REGEX_FLOAT_ARG.search(cmdArgs[index])
            if (match is not None):
                label = match.group("label").upper()
                value = float(match.group("value"))
                if (label == "X"):
                    x = position.X_AXIS.logicalToNative(value)
                elif (label == "Y"):
                    y = position.Y_AXIS.logicalToNative(value)
                elif (label == "Z"):
                    z = position.Z_AXIS.logicalToNative(value)
                elif (label == "E"):
                    extruderPosition = position.E_AXIS.logicalToNative(value)
                elif (label == "F"):
                    feedRate = value
                elif (label == "R"):
                    radius = value
                if (label == "I"):
                    i = position.X_AXIS.logicalToNative(value)
                if (label == "J"):
                    j = position.Y_AXIS.logicalToNative(value)

        # Based on Marlin 1.1.8
        if (radius is not None):
            (i, j) = self._computeCenterOffsets(x, y, radius, clockwise)

        if (i or j):
            xyPairs = self.planArc(x, y, i, j, clockwise)
            return self.state.processLinearMoves(cmd, extruderPosition, feedRate, z, *xyPairs)

        return None

    def _handle_G3(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """
        G3 - Controlled Arc Move (Counter-Clockwise).

        G3 [E<pos>] [F<rate>] R<radius> [X<pos>] [Y<pos>] [Z<pos>]
        G3 [E<pos>] [F<rate>] I<offset> J<offset> [X<pos>] [Y<pos>] [Z<pos>]
        """
        return self._handle_G2(cmd, gcode, subcode)

    def _handle_G10(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """
        G10 [S0 or S1] - Retract (if no P or L parameter).

        S parameter is for Repetier (0 = short retract, 1 = long retract)
        Existence of a P or L parameter indicates RepRap tool offset/temperature or workspace
        coordinates and is simply passed through unfiltered
        """
        cmdArgs = REGEX_SPLIT.split(cmd)
        for index in range(1, len(cmdArgs)):
            argType = cmdArgs[index][0].upper()
            if (argType == "P") or (argType == "L"):
                return None

        self._logger.debug("_handle_G10: firmware retraction: cmd=%s", cmd)
        returnCommands = self.state.recordRetraction(
            RetractionState(
                firmwareRetract=True,
                originalCommand=cmd
            ),
            None
        )

        if (returnCommands is None):
            return self.state.ignoreGcodeCommand()

        return returnCommands

    def _handle_G11(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """
        G11 [S0 or S1] - Recover (unretract).

        S parameter is for Repetier (0 = short unretract, 1 = long unretract)
        """
        returnCommands = self.state.recoverRetractionIfNeeded(None, cmd, True)
        if (returnCommands is None):
            return self.state.ignoreGcodeCommand()

        return returnCommands

    def _handle_G20(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """G20 - Set units to inches."""
        self.state.setUnitMultiplier(INCHES_PER_MM)

    def _handle_G21(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """G21 - Set units to millimeters."""
        self.state.setUnitMultiplier(1)

    def _handle_G28(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """
        G28 - Auto home.

        G28 [X] [Y] [Z]
        Set the current position to 0 for each axis in the command
        """
        position = self.state.position
        cmdArgs = REGEX_SPLIT.split(cmd)
        homeX = False
        homeY = False
        homeZ = False
        for arg in cmdArgs:
            arg = arg.upper()
            if (arg.startswith("X")):
                homeX = True
            elif (arg.startswith("Y")):
                homeY = True
            elif (arg.startswith("Z")):
                homeZ = True

        if (not (homeX or homeY or homeZ)):
            homeX = True
            homeY = True
            homeZ = True

        if (homeX):
            position.X_AXIS.setHome()

        if (homeY):
            position.Y_AXIS.setHome()

        if (homeZ):
            position.Z_AXIS.setHome()

    def _handle_G90(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """G90 - Set absolute positioning mode."""
        self.state.setAbsoluteMode(True)

    def _handle_G91(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """G91 - Set relative positioning mode."""
        self.state.setAbsoluteMode(False)

    def _handle_G92(self, cmd, gcode, subcode=None):  # pylint: disable=unused-argument,invalid-name
        """
        G92 - Set current position.

        G92 [E<pos>] [X<pos>] [Y<pos>] [Z<pos>]
        The hotend isn't actually moved, this command just changes where the firmware thinks it is
        by defining a coordinate offset.
        """
        position = self.state.position
        cmdArgs = REGEX_SPLIT.split(cmd)
        for index in range(1, len(cmdArgs)):
            match = REGEX_FLOAT_ARG.search(cmdArgs[index])
            if (match is not None):
                label = match.group("label").upper()
                value = float(match.group("value"))
                if (label == "E"):
                    # Note: 1.0 Marlin and earlier stored an offset for E instead of directly
                    #   updating the position.
                    # This assumes the newer behavior
                    position.E_AXIS.setLogicalPosition(value)
                elif (label == "X"):
                    position.X_AXIS.setLogicalOffsetPosition(value)
                elif (label == "Y"):
                    position.Y_AXIS.setLogicalOffsetPosition(value)
                elif (label == "Z"):
                    position.Z_AXIS.setLogicalOffsetPosition(value)

    def _handle_M206(self, cmd, gcode, subcode=None):  # nopep8 pylint: disable=unused-argument,invalid-name
        """
        M206 - Set home offsets.

        M206 [P<offset>] [T<offset>] [X<offset>] [Y<offset>] [Z<offset>]
        """
        position = self.state.position
        cmdArgs = REGEX_SPLIT.split(cmd)
        for index in range(1, len(cmdArgs)):
            match = REGEX_FLOAT_ARG.search(cmdArgs[index])
            if (match is not None):
                label = match.group("label").upper()
                value = float(match.group("value"))
                if (label == "X"):
                    position.X_AXIS.setHomeOffset(value)
                elif (label == "Y"):
                    position.Y_AXIS.setHomeOffset(value)
                elif (label == "Z"):
                    position.Z_AXIS.setHomeOffset(value)

    def handleAtCommand(self, commInstance, cmd, parameters):
        """
        Process registered At-Command actions.

        Parameters
        ----------
        commInstance : octoprint.util.comm.MachineCom
            The MachineCom instance to use for sending any Gcode commands produced
        cmd : string
            The At-Command that was encountered
        parameters : string
            The parameters provided for the At-Command
        """
        entries = self.state.atCommandActions.get(cmd)
        if (entries is not None):
            for entry in entries:
                if (entry.matches(cmd, parameters)):
                    self._logger.debug(
                        "handleAtCommand: processing At-Command: action=%s, cmd=%s, parameters=%s",
                        entry.action, cmd, parameters
                    )

                    if (entry.action == ENABLE_EXCLUSION):
                        self.state.enableExclusion(cmd + " " + parameters)
                    elif (entry.action == DISABLE_EXCLUSION):
                        for command in self.state.disableExclusion(cmd + " " + parameters):
                            self._logger.debug(
                                "handleAtCommand: sending Gcode command to printer: cmd=%s",
                                command
                            )
                            commInstance.sendCommand(command)
