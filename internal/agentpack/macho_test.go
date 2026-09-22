package agentpack

import (
	"encoding/binary"
	"testing"
)

// TestMachOSignatureDetection supports both unsigned Intel and ad-hoc signed Apple Silicon stubs.
func TestMachOSignatureDetection(t *testing.T) {
	unsigned := make([]byte, 32)
	binary.LittleEndian.PutUint32(unsigned[0:4], 0xfeedfacf)
	binary.LittleEndian.PutUint32(unsigned[4:8], 0x1000007)
	binary.LittleEndian.PutUint32(unsigned[12:16], 2)
	if HasMachOSignature(unsigned) || HasMachOSignature([]byte("ELF")) {
		t.Fatal("unsigned launcher requires signature removal")
	}
	signed := append(append([]byte{}, unsigned...), make([]byte, 24)...)
	binary.LittleEndian.PutUint32(signed[16:20], 1)
	binary.LittleEndian.PutUint32(signed[20:24], 16)
	binary.LittleEndian.PutUint32(signed[32:36], 0x1d)
	binary.LittleEndian.PutUint32(signed[36:40], 16)
	binary.LittleEndian.PutUint32(signed[40:44], 48)
	binary.LittleEndian.PutUint32(signed[44:48], 8)
	if !HasMachOSignature(signed) {
		t.Fatal("signed launcher was not detected")
	}
}
