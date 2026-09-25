class GrokGeneralError(Exception):
    exit_code = 1


class ValidationError(GrokGeneralError, ValueError):
    exit_code = 2


class NotFoundError(GrokGeneralError):
    exit_code = 3


class SafetyBlockedError(GrokGeneralError):
    exit_code = 4


class ProviderUnavailableError(GrokGeneralError):
    exit_code = 5


class StateError(GrokGeneralError):
    exit_code = 6
