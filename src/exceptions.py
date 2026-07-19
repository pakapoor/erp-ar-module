class PeriodClosedException(Exception):
    def __init__(self, message: str):
        self.message = message


class IdempotencyConflictException(Exception):
    def __init__(self, message: str, status: str):
        self.message = message
        self.status = status


class BusinessRuleException(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message


class VersionConflictException(Exception):
    def __init__(self, message: str):
        self.message = message
