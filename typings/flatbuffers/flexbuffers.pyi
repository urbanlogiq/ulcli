"""
This type stub file was generated by pyright.
"""

import contextlib
import enum

"""Implementation of FlexBuffers binary format.

For more info check https://google.github.io/flatbuffers/flexbuffers.html and
corresponding C++ implementation at
https://github.com/google/flatbuffers/blob/master/include/flatbuffers/flexbuffers.h
"""
__all__ = ("Type", "Builder", "GetRoot", "Dumps", "Loads")

class BitWidth(enum.IntEnum):
    """Supported bit widths of value types.

    These are used in the lower 2 bits of a type field to determine the size of
    the elements (and or size field) of the item pointed to (e.g. vector).
    """

    W8 = ...
    W16 = ...
    W32 = ...
    W64 = ...
    @staticmethod
    def U(value):  # -> Literal[BitWidth.W8, BitWidth.W16, BitWidth.W32, BitWidth.W64]:
        """Returns the minimum `BitWidth` to encode unsigned integer value."""
        ...
    @staticmethod
    def I(value):  # -> Literal[BitWidth.W8, BitWidth.W16, BitWidth.W32, BitWidth.W64]:
        """Returns the minimum `BitWidth` to encode signed integer value."""
        ...
    @staticmethod
    def F(value):  # -> Literal[BitWidth.W32, BitWidth.W64]:
        """Returns the `BitWidth` to encode floating point value."""
        ...
    @staticmethod
    def B(byte_width): ...

I = ...
U = ...
F = ...

class Type(enum.IntEnum):
    """Supported types of encoded data.

    These are used as the upper 6 bits of a type field to indicate the actual
    type.
    """

    NULL = ...
    INT = ...
    UINT = ...
    FLOAT = ...
    KEY = ...
    STRING = ...
    INDIRECT_INT = ...
    INDIRECT_UINT = ...
    INDIRECT_FLOAT = ...
    MAP = ...
    VECTOR = ...
    VECTOR_INT = ...
    VECTOR_UINT = ...
    VECTOR_FLOAT = ...
    VECTOR_KEY = ...
    VECTOR_STRING_DEPRECATED = ...
    VECTOR_INT2 = ...
    VECTOR_UINT2 = ...
    VECTOR_FLOAT2 = ...
    VECTOR_INT3 = ...
    VECTOR_UINT3 = ...
    VECTOR_FLOAT3 = ...
    VECTOR_INT4 = ...
    VECTOR_UINT4 = ...
    VECTOR_FLOAT4 = ...
    BLOB = ...
    BOOL = ...
    VECTOR_BOOL = ...
    @staticmethod
    def Pack(type_, bit_width): ...
    @staticmethod
    def Unpack(packed_type): ...
    @staticmethod
    def IsInline(type_): ...
    @staticmethod
    def IsTypedVector(type_): ...
    @staticmethod
    def IsTypedVectorElementType(type_): ...
    @staticmethod
    def ToTypedVectorElementType(type_): ...
    @staticmethod
    def IsFixedTypedVector(type_): ...
    @staticmethod
    def IsFixedTypedVectorElementType(type_): ...
    @staticmethod
    def ToFixedTypedVectorElementType(type_): ...
    @staticmethod
    def ToTypedVector(element_type, fixed_len=...):  # -> Type:
        """Converts element type to corresponding vector type.

        Args:
          element_type: vector element type
          fixed_len: number of elements: 0 for typed vector; 2, 3, or 4 for fixed
            typed vector.

        Returns:
          Typed vector type or fixed typed vector type.
        """
        ...

class Buf:
    """Class to access underlying buffer object starting from the given offset."""

    def __init__(self, buf, offset) -> None: ...
    def __getitem__(self, key): ...
    def __setitem__(self, key, value): ...
    def __repr__(self): ...
    def Find(self, sub):
        """Returns the lowest index where the sub subsequence is found."""
        ...
    def Slice(self, offset):  # -> Buf:
        """Returns new `Buf` which starts from the given offset."""
        ...
    def Indirect(self, offset, byte_width):  # -> Buf:
        """Return new `Buf` based on the encoded offset (indirect encoding)."""
        ...

class Object:
    """Base class for all non-trivial data accessors."""

    __slots__ = ...
    def __init__(self, buf, byte_width) -> None: ...
    @property
    def ByteWidth(self): ...

class Sized(Object):
    """Base class for all data accessors which need to read encoded size."""

    __slots__ = ...
    def __init__(self, buf, byte_width, size=...) -> None: ...
    @property
    def SizeBytes(self): ...
    def __len__(self): ...

class Blob(Sized):
    """Data accessor for the encoded blob bytes."""

    __slots__ = ...
    @property
    def Bytes(self): ...
    def __repr__(self): ...

class String(Sized):
    """Data accessor for the encoded string bytes."""

    __slots__ = ...
    @property
    def Bytes(self): ...
    def Mutate(self, value):  # -> bool:
        """Mutates underlying string bytes in place.

        Args:
          value: New string to replace the existing one. New string must have less
            or equal UTF-8-encoded bytes than the existing one to successfully
            mutate underlying byte buffer.

        Returns:
          Whether the value was mutated or not.
        """
        ...
    def __str__(self) -> str: ...
    def __repr__(self): ...

class Key(Object):
    """Data accessor for the encoded key bytes."""

    __slots__ = ...
    def __init__(self, buf, byte_width) -> None: ...
    @property
    def Bytes(self): ...
    def __len__(self): ...
    def __str__(self) -> str: ...
    def __repr__(self): ...

class Vector(Sized):
    """Data accessor for the encoded vector bytes."""

    __slots__ = ...
    def __getitem__(self, index): ...
    @property
    def Value(self):  # -> list[Any]:
        """Returns the underlying encoded data as a list object."""
        ...
    def __repr__(self): ...

class TypedVector(Sized):
    """Data accessor for the encoded typed vector or fixed typed vector bytes."""

    __slots__ = ...
    def __init__(self, buf, byte_width, element_type, size=...) -> None: ...
    @property
    def Bytes(self): ...
    @property
    def ElementType(self): ...
    def __getitem__(self, index): ...
    @property
    def Value(self):  # -> list[Any] | list[bool] | list[str]:
        """Returns underlying data as list object."""
        ...
    def __repr__(self): ...

class Map(Vector):
    """Data accessor for the encoded map bytes."""

    @staticmethod
    def CompareKeys(a, b): ...
    def __getitem__(self, key): ...
    @property
    def Keys(self): ...
    @property
    def Values(self): ...
    @property
    def Value(self): ...
    def __repr__(self): ...

class Ref:
    """Data accessor for the encoded data bytes."""

    __slots__ = ...
    @staticmethod
    def PackedType(buf, parent_width, packed_type): ...
    def __init__(self, buf, parent_width, byte_width, type_) -> None: ...
    def __repr__(self): ...
    @property
    def IsNull(self): ...
    @property
    def IsBool(self): ...
    @property
    def AsBool(self): ...
    def MutateBool(self, value):  # -> bool:
        """Mutates underlying boolean value bytes in place.

        Args:
          value: New boolean value.

        Returns:
          Whether the value was mutated or not.
        """
        ...
    @property
    def IsNumeric(self): ...
    @property
    def IsInt(self): ...
    @property
    def AsInt(self):  # -> int | Any:
        """Returns current reference as integer value."""
        ...
    def MutateInt(self, value):  # -> bool:
        """Mutates underlying integer value bytes in place.

        Args:
          value: New integer value. It must fit to the byte size of the existing
            encoded value.

        Returns:
          Whether the value was mutated or not.
        """
        ...
    @property
    def IsFloat(self): ...
    @property
    def AsFloat(self):  # -> float | Any:
        """Returns current reference as floating point value."""
        ...
    def MutateFloat(self, value):  # -> bool:
        """Mutates underlying floating point value bytes in place.

        Args:
          value: New float value. It must fit to the byte size of the existing
            encoded value.

        Returns:
          Whether the value was mutated or not.
        """
        ...
    @property
    def IsKey(self): ...
    @property
    def AsKeyBytes(self): ...
    @property
    def AsKey(self): ...
    @property
    def IsString(self): ...
    @property
    def AsStringBytes(self): ...
    @property
    def AsString(self): ...
    def MutateString(self, value): ...
    @property
    def IsBlob(self): ...
    @property
    def AsBlob(self): ...
    @property
    def IsAnyVector(self): ...
    @property
    def IsVector(self): ...
    @property
    def AsVector(self): ...
    @property
    def IsTypedVector(self): ...
    @property
    def AsTypedVector(self): ...
    @property
    def IsFixedTypedVector(self): ...
    @property
    def AsFixedTypedVector(self): ...
    @property
    def IsMap(self): ...
    @property
    def AsMap(self): ...
    @property
    def Value(
        self,
    ):  # -> bool | Any | int | float | str | dict[Any, Any] | list[Any] | list[bool] | list[str] | None:
        """Converts current reference to value of corresponding type.

        This is equivalent to calling `AsInt` for integer values, `AsFloat` for
        floating point values, etc.

        Returns:
          Value of corresponding type.
        """
        ...

class Value:
    """Class to represent given value during the encoding process."""

    @staticmethod
    def Null(): ...
    @staticmethod
    def Bool(value): ...
    @staticmethod
    def Int(value, bit_width): ...
    @staticmethod
    def UInt(value, bit_width): ...
    @staticmethod
    def Float(value, bit_width): ...
    @staticmethod
    def Key(offset): ...
    def __init__(self, value, type_, min_bit_width) -> None: ...
    @property
    def Value(self): ...
    @property
    def Type(self): ...
    @property
    def MinBitWidth(self): ...
    def StoredPackedType(self, parent_bit_width=...): ...
    def ElemWidth(self, buf_size, elem_index=...): ...
    def StoredWidth(self, parent_bit_width=...): ...
    def __repr__(self): ...
    def __str__(self) -> str: ...

def InMap(func): ...
def InMapForString(func): ...

class Pool:
    """Collection of (data, offset) pairs sorted by data for quick access."""

    def __init__(self) -> None: ...
    def FindOrInsert(self, data, offset): ...
    def Clear(self): ...
    @property
    def Elements(self): ...

class Builder:
    """Helper class to encode structural data into flexbuffers format."""

    def __init__(
        self, share_strings=..., share_keys=..., force_min_bit_width=...
    ) -> None: ...
    def __len__(self): ...
    @property
    def StringPool(self): ...
    @property
    def KeyPool(self): ...
    def Clear(self): ...
    def Finish(self):  # -> bytearray:
        """Finishes encoding process and returns underlying buffer."""
        ...
    @InMapForString
    def String(self, value):  # -> int:
        """Encodes string value."""
        ...
    @InMap
    def Blob(self, value):  # -> int:
        """Encodes binary blob value.

        Args:
          value: A byte/bytearray value to encode

        Returns:
          Offset of the encoded value in underlying the byte buffer.
        """
        ...
    def Key(self, value):  # -> int:
        """Encodes key value.

        Args:
          value: A byte/bytearray/str value to encode. Byte object must not contain
            zero bytes. String object must be convertible to ASCII.

        Returns:
          Offset of the encoded value in the underlying byte buffer.
        """
        ...
    def Null(self, key=...):  # -> None:
        """Encodes None value."""
        ...
    @InMap
    def Bool(self, value):  # -> None:
        """Encodes boolean value.

        Args:
          value: A boolean value.
        """
        ...
    @InMap
    def Int(self, value, byte_width=...):  # -> None:
        """Encodes signed integer value.

        Args:
          value: A signed integer value.
          byte_width: Number of bytes to use: 1, 2, 4, or 8.
        """
        ...
    @InMap
    def IndirectInt(self, value, byte_width=...):  # -> None:
        """Encodes signed integer value indirectly.

        Args:
          value: A signed integer value.
          byte_width: Number of bytes to use: 1, 2, 4, or 8.
        """
        ...
    @InMap
    def UInt(self, value, byte_width=...):  # -> None:
        """Encodes unsigned integer value.

        Args:
          value: An unsigned integer value.
          byte_width: Number of bytes to use: 1, 2, 4, or 8.
        """
        ...
    @InMap
    def IndirectUInt(self, value, byte_width=...):  # -> None:
        """Encodes unsigned integer value indirectly.

        Args:
          value: An unsigned integer value.
          byte_width: Number of bytes to use: 1, 2, 4, or 8.
        """
        ...
    @InMap
    def Float(self, value, byte_width=...):  # -> None:
        """Encodes floating point value.

        Args:
          value: A floating point value.
          byte_width: Number of bytes to use: 4 or 8.
        """
        ...
    @InMap
    def IndirectFloat(self, value, byte_width=...):  # -> None:
        """Encodes floating point value indirectly.

        Args:
          value: A floating point value.
          byte_width: Number of bytes to use: 4 or 8.
        """
        ...
    @contextlib.contextmanager
    def Vector(self, key=...): ...
    @InMap
    def VectorFromElements(self, elements):  # -> None:
        """Encodes sequence of any elements as a vector.

        Args:
          elements: sequence of elements, they may have different types.
        """
        ...
    @contextlib.contextmanager
    def TypedVector(self, key=...): ...
    @InMap
    def TypedVectorFromElements(self, elements, element_type=...):  # -> None:
        """Encodes sequence of elements of the same type as typed vector.

        Args:
          elements: Sequence of elements, they must be of the same type.
          element_type: Suggested element type. Setting it to None means determining
            correct value automatically based on the given elements.
        """
        ...
    @InMap
    def FixedTypedVectorFromElements(
        self, elements, element_type=..., byte_width=...
    ):  # -> None:
        """Encodes sequence of elements of the same type as fixed typed vector.

        Args:
          elements: Sequence of elements, they must be of the same type. Allowed
            types are `Type.INT`, `Type.UINT`, `Type.FLOAT`. Allowed number of
            elements are 2, 3, or 4.
          element_type: Suggested element type. Setting it to None means determining
            correct value automatically based on the given elements.
          byte_width: Number of bytes to use per element. For `Type.INT` and
            `Type.UINT`: 1, 2, 4, or 8. For `Type.FLOAT`: 4 or 8. Setting it to 0
            means determining correct value automatically based on the given
            elements.
        """
        ...
    @contextlib.contextmanager
    def Map(self, key=...): ...
    def MapFromElements(self, elements): ...
    def Adder(self, type_): ...
    @InMapForString
    def Add(self, value):  # -> None:
        """Encodes value of any supported type."""
        ...
    @property
    def LastValue(self): ...
    @InMap
    def ReuseValue(self, value): ...

def GetRoot(buf):  # -> Ref:
    """Returns root `Ref` object for the given buffer."""
    ...

def Dumps(obj):  # -> bytearray:
    """Returns bytearray with the encoded python object."""
    ...

def Loads(
    buf,
):  # -> bool | Any | int | float | str | dict[Any, Any] | list[Any] | list[bool] | list[str] | None:
    """Returns python object decoded from the buffer."""
    ...
