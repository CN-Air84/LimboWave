"""Input DTOs deliberately exclude tool approvals, credentials and origin overrides."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Id = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PairRequest(StrictModel):
    ticket: Annotated[str | None, Field(max_length=128)] = None
    code: Annotated[str | None, Field(pattern=r"^[0-9]{8}$")] = None
    name: Annotated[str, Field(min_length=1, max_length=80)] = "Browser"


class CommandBase(StrictModel):
    server_epoch: Id
    client_command_id: Id
    expected_revision: Annotated[int, Field(ge=0)]


class NewSession(CommandBase):
    type: Literal["new_session"]


class Send(CommandBase):
    type: Literal["send"]
    conversation_id: Id
    branch_id: Id
    text: Annotated[str, Field(min_length=1, max_length=32768)]
    model_id: Id | None = None


class Abort(CommandBase):
    type: Literal["abort"]
    run_id: Id


class SelectModel(CommandBase):
    type: Literal["select_model"]
    model_id: Id


Command = Annotated[NewSession | Send | Abort | SelectModel, Field(discriminator="type")]


class PasswordEnvelope(StrictModel):
    challenge_id: Annotated[str, Field(min_length=1, max_length=128)]
    encrypted_key: Annotated[str, Field(min_length=1, max_length=512)]
    iv: Annotated[str, Field(min_length=1, max_length=32)]
    ciphertext: Annotated[str, Field(min_length=1, max_length=5500)]
