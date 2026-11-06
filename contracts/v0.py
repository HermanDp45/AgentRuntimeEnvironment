from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    WithJsonSchema,
)

from contracts.paths import PATH_PATTERN, safe_path

SafePath = Annotated[
    str,
    Field(min_length=1, max_length=512),
    AfterValidator(safe_path),
    WithJsonSchema({"type": "string", "minLength": 1, "maxLength": 512, "pattern": PATH_PATTERN}),
]
Digest = Annotated[str, Field(pattern=r"^(missing|[0-9a-f]{64})$")]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["0"]


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["command", "timeout", "sandbox", "transport", "model", "limit"]
    code: str
    message: str


class ReadFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["ReadFile"]
    path: SafePath


class WriteFile(ReadFile):
    kind: Literal["WriteFile"]
    content: str = Field(max_length=65536)
    expected_sha256: Digest


class RunCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["RunCommand"]
    command: str = Field(min_length=1, max_length=8192)


Operation = Annotated[ReadFile | WriteFile | RunCommand, Field(discriminator="kind")]


class Provision(Message):
    type: Literal["provision"]
    session_id: UUID
    files: dict[SafePath, Annotated[str, Field(max_length=65536)]] = Field(
        default_factory=dict, max_length=128
    )
    read_only: StrictBool = False


class Exec(Message):
    type: Literal["exec"]
    session_id: UUID
    operation_id: UUID
    operation: Operation
    timeout_s: float = Field(default=120, gt=0, le=3600)


class Collect(Message):
    type: Literal["collect"]
    session_id: UUID


class Release(Message):
    type: Literal["release"]
    session_id: UUID


class Cancel(Message):
    type: Literal["cancel"]
    operation_id: UUID


class Session(Message):
    type: Literal["session"]
    session_id: UUID
    read_only: bool


class Released(Message):
    type: Literal["released"]
    session_id: UUID


class FileData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str
    sha256: Digest


class Collection(Message):
    type: Literal["collection"]
    session_id: UUID
    files: dict[str, FileData]


class ErrorReply(Message):
    type: Literal["error"]
    error: ErrorInfo


class Success(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"]
    value: dict[str, JsonValue] = Field(default_factory=dict)


class Failed(Success):
    status: Literal["error"]
    error: ErrorInfo


class Cancelled(Success):
    status: Literal["cancelled"]


Result = Annotated[Success | Failed | Cancelled, Field(discriminator="status")]


class OperationState(Message):
    type: Literal["operation"]
    session_id: UUID
    operation_id: UUID
    state: Literal["running", "finished"]
    result: Result | None = None


class EventBase(Message):
    session_id: UUID
    operation_id: UUID
    sequence: int = Field(ge=1)


class Started(EventBase):
    type: Literal["started"]


class Output(EventBase):
    type: Literal["stdout", "stderr"]
    text: str


class Finished(EventBase):
    type: Literal["result"]
    result: Result


Event = Annotated[Started | Output | Finished, Field(discriminator="type")]
SandboxMessage = Annotated[
    Provision
    | Exec
    | Collect
    | Release
    | Cancel
    | Session
    | Released
    | Collection
    | ErrorReply
    | OperationState
    | Started
    | Output
    | Finished,
    Field(discriminator="type"),
]


class ClientSession(Message):
    type: Literal["session.create"]
    session_id: UUID
    folder: str = Field(min_length=1, max_length=4096)
    read_only: StrictBool = False


class FixtureSession(Provision):
    type: Literal["session.fixture"]


class ClientTask(Message):
    type: Literal["task"]
    session_id: UUID
    run_id: UUID
    message: str = Field(min_length=1, max_length=65536)


class Approval(Message):
    type: Literal["approval"]
    session_id: UUID
    run_id: UUID
    operation_id: UUID
    approved: StrictBool


class ClientCancel(Message):
    type: Literal["run.cancel"]
    session_id: UUID
    run_id: UUID


class Resume(ClientCancel):
    type: Literal["resume"]
    after: int = Field(default=0, ge=0)


class ClientClose(Message):
    type: Literal["session.close"]
    session_id: UUID


class RunEvent(Message):
    type: Literal["run.event"]
    session_id: UUID
    run_id: UUID
    sequence: int = Field(ge=1)
    event: Literal["message", "tool", "approval_required", "status"]
    data: dict[str, JsonValue]


class RunResult(Message):
    type: Literal["run.result"]
    session_id: UUID
    run_id: UUID
    sequence: int = Field(ge=1)
    result: Result


class RunState(Message):
    type: Literal["run.state"]
    session_id: UUID
    run_id: UUID
    state: Literal["running", "waiting_approval", "finished"]
    pending_operation_id: UUID | None = None
    result: Result | None = None


ClientMessage = Annotated[
    ClientSession
    | FixtureSession
    | ClientTask
    | Approval
    | ClientCancel
    | Resume
    | ClientClose
    | Session
    | Collection
    | Released
    | RunEvent
    | RunResult
    | RunState
    | ErrorReply,
    Field(discriminator="type"),
]
